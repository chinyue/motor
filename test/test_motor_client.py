# Copyright 2012 10gen, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Test Motor, an asynchronous driver for MongoDB and Tornado."""

import os
import socket
import time
import unittest
import sys

from nose.plugins.skip import SkipTest
import pymongo
from pymongo.errors import ConfigurationError
from pymongo.errors import ConnectionFailure
from tornado import gen
from tornado.concurrent import Future
from tornado.ioloop import IOLoop
from tornado.testing import gen_test

import motor
from test import host, port, assert_raises, MotorTest
from test.utils import server_is_master_with_slave, delay
from test.utils import server_started_with_auth


class MotorClientTest(MotorTest):
    @gen_test
    def test_client_open(self):
        cx = motor.MotorClient(host, port, io_loop=self.io_loop)
        result = yield cx.open()
        self.assertEqual(result, cx)

        # Ensure future is marked done if already connected.
        self.assertEqual(cx, (yield cx.open()))
        cx.close()

    @gen_test
    def test_client_lazy_connect(self):
        # TODO: insert update save find remove command.
        self.sync_cx.pymongo_test.test_client_lazy_connect.remove()

        # Create client without connecting; connect on demand.
        cx = motor.MotorClient(host, port, io_loop=self.io_loop)
        collection = cx.pymongo_test.test_client_lazy_connect
        future0 = collection.insert({'foo': 'bar'})
        future1 = collection.insert({'foo': 'bar'})
        yield [future0, future1]

        self.assertEqual(2, (yield collection.find({'foo': 'bar'}).count()))

        cx.close()

    @gen_test
    def test_disconnect(self):
        cx = self.motor_client()
        cx.disconnect()
        self.assertEqual(0, len(cx.delegate._MongoClient__pool.sockets))

    @gen_test
    def test_unix_socket(self):
        if not hasattr(socket, "AF_UNIX"):
            raise SkipTest("UNIX-sockets are not supported on this system")

        if (sys.platform == 'darwin' and
                server_started_with_auth(self.sync_cx)):
            raise SkipTest("SERVER-8492")

        mongodb_socket = '/tmp/mongodb-27017.sock'
        if not os.access(mongodb_socket, os.R_OK):
            raise SkipTest("Socket file is not accessible")

        yield motor.MotorClient(
            "mongodb://%s" % mongodb_socket, io_loop=self.io_loop).open()

        client = yield motor.MotorClient(
            "mongodb://%s" % mongodb_socket, io_loop=self.io_loop).open()

        yield client.pymongo_test.test.save({"dummy": "object"})

        # Confirm we can read via the socket.
        dbs = yield client.database_names()
        self.assertTrue("pymongo_test" in dbs)
        client.close()

        # Confirm it fails with a missing socket.
        client = motor.MotorClient(
            "mongodb:///tmp/non-existent.sock", io_loop=self.io_loop)

        with assert_raises(ConnectionFailure):
            yield client.open()

    def test_io_loop(self):
        with assert_raises(TypeError):
            motor.MotorClient(host, port, io_loop='foo')

    def test_open_sync(self):
        loop = IOLoop()
        cx = loop.run_sync(motor.MotorClient(host, port, io_loop=loop).open)
        self.assertTrue(isinstance(cx, motor.MotorClient))

    def test_database_named_delegate(self):
        self.assertTrue(
            isinstance(self.cx.delegate, pymongo.mongo_client.MongoClient))
        self.assertTrue(isinstance(self.cx['delegate'], motor.MotorDatabase))

    @gen_test
    def test_copy_db_argument_checking(self):
        with assert_raises(TypeError):
            yield self.cx.copy_database(4, "foo")

        with assert_raises(TypeError):
            yield self.cx.copy_database("foo", 4)

        with assert_raises(pymongo.errors.InvalidName):
            yield self.cx.copy_database("foo", "$foo")

    def drop_databases(self, database_names):
        for test_db_name in database_names:
            # Setup code has configured a short timeout, and the copying
            # has put Mongo under enough load that we risk timeouts here
            # unless we override. command() takes no network_timeout but
            # find_one does.
            self.sync_cx[test_db_name]['$cmd'].find_one(
                {'dropDatabase': 1}, network_timeout=30)

        # Due to SERVER-2329, databases may not disappear from a master
        # in a master-slave pair.
        if not server_is_master_with_slave(self.sync_cx):
            start = time.time()
            
            # There may be a race condition in the server's dropDatabase. Wait
            # for it to update its namespaces.
            db_names = self.sync_cx.database_names()
            while time.time() - start < 10:
                remaining_test_dbs = (
                    set(database_names).intersection(db_names))
                
                if not remaining_test_dbs:
                    # All test DBs are removed.
                    break

                db_names = self.sync_cx.database_names()
                
            for test_db_name in database_names:
                self.assertFalse(
                    test_db_name in db_names,
                    "%s not dropped" % test_db_name)

    @gen_test(timeout=300)
    def test_copy_db(self):
        # 1. Drop old DBs
        # 2. Copy a DB N times at once, to test for concurrency bugs
        # 3. Create a username and password
        # 4. Copy a database using name and password
        ncopies = 10
        test_db_names = ['pymongo_test%s' % i for i in range(ncopies)]

        def check_copydb_results():
            db_names = self.sync_cx.database_names()
            for test_db_name in test_db_names:
                self.assertTrue(test_db_name in db_names)
                result = self.sync_cx[test_db_name].test_collection.find_one()
                self.assertTrue(result, "No results in %s" % test_db_name)
                self.assertEqual(
                    "bar", result.get("foo"),
                    "Wrong result from %s: %s" % (test_db_name, result))

        # 1. Drop old test DBs
        yield self.cx.drop_database('pymongo_test')
        self.drop_databases(test_db_names)

        # 2. Copy a test DB N times at once
        yield self.cx.pymongo_test.test_collection.insert({"foo": "bar"})
        yield [
            self.cx.copy_database("pymongo_test", test_db_name)
            for test_db_name in test_db_names]

        check_copydb_results()
        self.drop_databases(test_db_names)

        # 3. Create a username and password
        yield self.cx.pymongo_test.add_user("mike", "password")

        with assert_raises(pymongo.errors.OperationFailure):
            yield self.cx.copy_database(
                "pymongo_test", "pymongo_test0",
                username="foo", password="bar")

        with assert_raises(pymongo.errors.OperationFailure):
            yield self.cx.copy_database(
                "pymongo_test", "pymongo_test0",
                username="mike", password="bar")

        # 4. Copy a database using name and password
        if not self.cx.is_mongos:
            # See SERVER-6427
            yield [
                self.cx.copy_database(
                    "pymongo_test", test_db_name,
                    username="mike", password="password")
                for test_db_name in test_db_names]

            check_copydb_results()

        self.drop_databases(test_db_names)

    @gen_test
    def test_timeout(self):
        # Launch two slow find_ones. The one with a timeout should get an error
        no_timeout = self.motor_client()
        timeout = self.motor_client(host, port, socketTimeoutMS=100)
        query = {'$where': delay(0.5), '_id': 1}

        timeout_fut = timeout.pymongo_test.test_collection.find_one(query)
        notimeout_fut = no_timeout.pymongo_test.test_collection.find_one(query)

        error = None
        try:
            yield [timeout_fut, notimeout_fut]
        except pymongo.errors.AutoReconnect, e:
            error = e

        self.assertEqual(str(error), 'timed out')
        self.assertEqual({'_id': 1, 's': hex(1)}, notimeout_fut.result())
        no_timeout.close()
        timeout.close()

    @gen_test
    def test_connection_failure(self):
        # Assuming there isn't anything actually running on this port
        client = motor.MotorClient('localhost', 8765, io_loop=self.io_loop)
        with assert_raises(ConnectionFailure):
            yield client.open()

    @gen_test
    def test_connection_timeout(self):
        # Motor merely tries to time out a connection attempt within the
        # specified duration; DNS lookup in particular isn't charged against
        # the timeout. So don't measure how long this takes.
        client = motor.MotorClient(
            'example.com', port=12345,
            connectTimeoutMS=1, io_loop=self.io_loop)

        with assert_raises(ConnectionFailure):
            yield client.open()

    @gen_test
    def test_max_pool_size_validation(self):
        with assert_raises(ConfigurationError):
            motor.MotorClient(host=host, port=port, max_pool_size=-1)

        with assert_raises(ConfigurationError):
            motor.MotorClient(host=host, port=port, max_pool_size='foo')

        cx = motor.MotorClient(
            host=host, port=port, max_pool_size=100, io_loop=self.io_loop)

        self.assertEqual(cx.max_pool_size, 100)
        cx.close()

    def test_requests(self):
        for method in 'start_request', 'in_request', 'end_request':
            self.assertRaises(TypeError, getattr(self.cx, method))

    @gen_test
    def test_high_concurrency(self):
        concurrency = 100
        cx = self.motor_client(max_pool_size=concurrency)
        self.sync_db.insert_collection.drop()
        self.assertEqual(200, self.sync_coll.count())
        expected_finds = 200 * concurrency
        n_inserts = 100

        collection = cx.pymongo_test.test_collection
        insert_collection = cx.pymongo_test.insert_collection

        ndocs = [0]
        insert_future = Future()

        @gen.coroutine
        def find():
            cursor = collection.find()
            while (yield cursor.fetch_next):
                cursor.next_object()
                ndocs[0] += 1

                # Half-way through, start an insert loop
                if ndocs[0] == expected_finds / 2:
                    insert()

        @gen.coroutine
        def insert():
            for i in range(n_inserts):
                yield insert_collection.insert({'s': hex(i)})

            insert_future.set_result(None)  # Finished

        yield [find() for _ in range(concurrency)]
        yield insert_future
        self.assertEqual(expected_finds, ndocs[0])
        self.assertEqual(n_inserts, self.sync_db.insert_collection.count())
        self.sync_db.insert_collection.drop()

    @gen_test
    def test_drop_database(self):
        # Make sure we can pass a MotorDatabase instance to drop_database
        db = self.cx.test_drop_database
        yield db.test_collection.insert({})
        names = yield self.cx.database_names()
        self.assertTrue('test_drop_database' in names)
        yield self.cx.drop_database(db)
        names = yield self.cx.database_names()
        self.assertFalse('test_drop_database' in names)


if __name__ == '__main__':
    unittest.main()

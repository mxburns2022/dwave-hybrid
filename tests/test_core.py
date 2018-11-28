# Copyright 2018 D-Wave Systems Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import unittest
import concurrent.futures
import logging
import itertools

import mock
import dimod
from tabu import TabuSampler

import hybrid
from hybrid.core import (
    PliableDict, State, SampleSet, HybridSampler,
    HybridRunnable, HybridProblemRunnable, HybridSubproblemRunnable)
from hybrid.decomposers import IdentityDecomposer
from hybrid.composers import IdentityComposer
from hybrid.samplers import TabuProblemSampler
from hybrid.utils import min_sample, sample_as_dict
from hybrid.testing import isolated_environ
from hybrid.exceptions import RunnableError


class TestPliableDict(unittest.TestCase):

    def test_construction(self):
        self.assertDictEqual(PliableDict(), {})
        self.assertDictEqual(PliableDict(x=1), {'x': 1})
        self.assertDictEqual(PliableDict(**{'x': 1}), {'x': 1})
        self.assertDictEqual(PliableDict({'x': 1, 'y': 2}), {'x': 1, 'y': 2})

    def test_setter(self):
        d = PliableDict()
        d.x = 1
        self.assertDictEqual(d, {'x': 1})

    def test_getter(self):
        d = PliableDict(x=1)
        self.assertEqual(d.x, 1)
        self.assertEqual(d.y, None)


class TestState(unittest.TestCase):

    def test_construction(self):
        self.assertDictEqual(State(), dict(samples=None, problem=None))
        self.assertEqual(State(samples=[1]).samples, [1])
        self.assertEqual(State(problem={'a': 1}).problem, {'a': 1})
        self.assertEqual(State(debug={'a': 1}).debug, {'a': 1})

    def test_from_samples(self):
        s1 = [0, 1]
        s2 = {0: 1, 1: 0}
        bqm = dimod.BinaryQuadraticModel({0: 1, 1: 2}, {}, 0.0, 'BINARY')
        self.assertEqual(State.from_sample(s1, bqm).samples.first.energy, 2.0)
        self.assertEqual(State.from_sample(s2, bqm).samples.first.energy, 1.0)
        self.assertEqual(State.from_samples([s1, s1], bqm).samples.first.energy, 2.0)
        self.assertEqual(State.from_samples([s2, s2], bqm).samples.first.energy, 1.0)
        self.assertEqual(State.from_samples([sample_as_dict(s1), s2], bqm).samples.first.energy, 1.0)

    def test_updated(self):
        a = SampleSet.from_samples([1,0,1], 'SPIN', 0)
        b = SampleSet.from_samples([0,1,0], 'SPIN', 0)
        s1 = State(samples=a)
        s2 = State(samples=b, emb={'a': {'b': 1}}, debug={'x': 1})
        s3 = State(debug={'x': {'y': {'z': [1]}}})

        # test simple replace
        self.assertDictEqual(s1.updated(), s1)
        self.assertDictEqual(s1.updated(samples=b), State(samples=b))
        self.assertDictEqual(s2.updated(emb={'b': 1}).emb, {'b': 1})
        self.assertDictEqual(s1.updated(samples=b, debug=dict(x=1), emb={'a': {'b': 1}}), s2)

        # test recursive merge of `debug`
        self.assertDictEqual(s1.updated(debug=dict(x=1)).debug, {'x': 1})
        self.assertDictEqual(s2.updated(debug=dict(x=2)).debug, {'x': 2})
        self.assertDictEqual(s2.updated(debug=dict(y=2)).debug, {'x': 1, 'y': 2})
        self.assertDictEqual(s2.updated(debug=dict(y=2)).debug, {'x': 1, 'y': 2})

        self.assertDictEqual(s3.updated(debug={'x': {'y': {'z': [2]}}}).debug, {'x': {'y': {'z': [2]}}})
        self.assertDictEqual(s3.updated(debug={'x': {'y': {'w': 2}}}).debug, {'x': {'y': {'z': [1], 'w': 2}}})

        # test clear
        self.assertEqual(s2.updated(emb=None).emb, None)
        self.assertEqual(s2.updated(debug=None).debug, None)


class TestHybridSampler(unittest.TestCase):

    def test_simple(self):
        bqm = dimod.BinaryQuadraticModel({}, {'ab': 1, 'bc': 1, 'ca': -1}, 0, dimod.SPIN)
        sampler = HybridSampler(TabuProblemSampler())
        response = sampler.sample(bqm)

        self.assertEqual(response.record[0].energy, -3.0)

    def test_validation(self):
        bqm = dimod.BinaryQuadraticModel({}, {'ab': 1, 'bc': 1, 'ca': -1}, 0, dimod.SPIN)
        sampler = TabuProblemSampler()

        with self.assertRaises(TypeError):
            HybridSampler()

        with self.assertRaises(TypeError):
            HybridSampler(sampler).sample(1)

        with self.assertRaises(ValueError):
            HybridSampler(sampler).sample(bqm, initial_sample={1: 2})

        response = HybridSampler(sampler).sample(bqm, initial_sample={'a': 1, 'b': 1, 'c': 1})
        self.assertEqual(response.record[0].energy, -3.0)

        response = HybridSampler(sampler).sample(bqm, initial_sample={'a': -1, 'b': 1, 'c': -1})
        self.assertEqual(response.record[0].energy, -3.0)


class TestHybridRunnable(unittest.TestCase):
    bqm = dimod.BinaryQuadraticModel({}, {'ab': 1, 'bc': 1, 'ca': -1}, 0, dimod.SPIN)
    init_state = State.from_sample(min_sample(bqm), bqm)

    def test_generic(self):
        runnable = HybridRunnable(TabuSampler(), fields=('problem', 'samples'))
        response = runnable.run(self.init_state)

        self.assertIsInstance(response, concurrent.futures.Future)
        self.assertEqual(response.result().samples.record[0].energy, -3.0)

    def test_problem_sampler_runnable(self):
        runnable = HybridProblemRunnable(TabuSampler())
        response = runnable.run(self.init_state)

        self.assertIsInstance(response, concurrent.futures.Future)
        self.assertEqual(response.result().samples.record[0].energy, -3.0)

    def test_subproblem_sampler_runnable(self):
        runnable = HybridSubproblemRunnable(TabuSampler())
        state = self.init_state.updated(subproblem=self.bqm)
        response = runnable.run(state)

        self.assertIsInstance(response, concurrent.futures.Future)
        self.assertEqual(response.result().subsamples.record[0].energy, -3.0)

    def test_runnable_composition(self):
        runnable = IdentityDecomposer() | HybridSubproblemRunnable(TabuSampler()) | IdentityComposer()
        response = runnable.run(self.init_state)

        self.assertIsInstance(response, concurrent.futures.Future)
        self.assertEqual(response.result().samples.record[0].energy, -3.0)


class TestLogging(unittest.TestCase):

    def test_init(self):
        self.assertTrue(hasattr(logging, 'TRACE'))
        self.assertEqual(getattr(logging, 'TRACE'), 5)

        logger = logging.getLogger(__name__)
        self.assertTrue(callable(logger.trace))

    def test_loglevel_from_env(self):
        logger = logging.getLogger(__name__)

        def ll_check(env, name):
            with isolated_environ(remove_dwave=True):
                os.environ[env] = name
                hybrid._apply_loglevel_from_env(logger)
                self.assertEqual(logger.getEffectiveLevel(), getattr(logging, name.upper()))

        levels = ('trace', 'debug', 'info', 'warning', 'error', 'critical')
        combinations = itertools.product(
            ['DWAVE_HYBRID_LOG_LEVEL', 'dwave_hybrid_log_level'],
            itertools.chain((l.lower() for l in levels), (l.upper() for l in levels)))

        for env, name in combinations:
            ll_check(env, name)

    def test_trace_logging(self):
        logger = logging.getLogger(__name__)
        hybrid._create_trace_loglevel(logging)  # force local _trace override

        with isolated_environ(remove_dwave=True):
            # trace on
            os.environ['DWAVE_HYBRID_LOG_LEVEL'] = 'trace'
            hybrid._apply_loglevel_from_env(logger)
            with mock.patch.object(logger, '_log') as m:
                logger.trace('test')
                m.assert_called_with(logging.TRACE, 'test', ())

            # trace off
            os.environ['DWAVE_HYBRID_LOG_LEVEL'] = 'debug'
            hybrid._apply_loglevel_from_env(logger)
            with mock.patch.object(logger, '_log') as m:
                logger.trace('test')
                self.assertFalse(m.called)


class TestExceptions(unittest.TestCase):

    def test_init(self):
        self.assertEqual(RunnableError('msg', 1).state, 1)

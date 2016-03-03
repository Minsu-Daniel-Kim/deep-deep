# -*- coding: utf-8 -*-
import itertools
import json
import os
import time
import random
import datetime

import networkx as nx
from twisted.internet.task import LoopingCall
from sklearn.linear_model import SGDRegressor
from sklearn.feature_extraction import DictVectorizer
import scrapy
from scipy import sparse

from deepdeep.queues import BalancedPriorityQueue, RequestsPriorityQueue, \
    FLOAT_PRIORITY_MULTIPLIER
from deepdeep.spiders.base import BaseSpider
from deepdeep.utils import (
    get_response_domain,
    set_request_domain,
    ensure_folder_exists,
    MaxScores,
    dict_subtract)
from deepdeep import score_links
from deepdeep.score_pages import (
    available_form_types,
    get_constant_scores,
    response_max_scores,
)


class TDRegressor:
    """
    Regression model for Q function. It uses Temporal Difference Learning.

    FIXME: it should work on vectors of observations.
    """
    def __init__(self, gamma):
        self.gamma = gamma
        self.Q = SGDRegressor(
            epsilon=1.0,
            shuffle=False,
            learning_rate="constant",
            n_iter=1,
            average=True,
        )
        self.action_vectorizer = score_links.get_vectorizer(
            use_hashing=True, use_domain=False
        )
        self.state_vectorizer = DictVectorizer()
        self.state_vectorizer.fit([get_constant_scores(0.0)])
        # self.vec = FeatureUnion([self.action_vectorizer, self.state_vectorier])

    def update(self, s_t, a_t, r_t1, s_t1, a_t1):
        X_t = self._vectorize(s_t, a_t)
        if a_t1 is None or self.Q.coef_ is None:
            future_reward = 0.0  # be optimistic
        else:
            X_t1 = self._vectorize(s_t1, a_t1)
            future_reward = self.gamma * self.Q.predict(X_t1)[0]

        R_observed = r_t1 + future_reward
        # print(self.predict(s_t, a_t), R_observed)
        print('Return: predicted={:0.3f}, observed={:0.3f}, immediate={:0.3f}, future={:0.3f}'.format(
            self.predict(s_t, a_t),
            R_observed,
            r_t1,
            future_reward,
        ))
        # print(s_t, a_t, r_t1, s_t1, a_t1)
        sample_weight = 1.0 # 20.0 if R_observed > 0.5 else 1.0
        self.Q.partial_fit(X_t, [R_observed], sample_weight=[sample_weight])

    def predict(self, s, a):
        if self.Q.coef_ is None:
            return 0.0
        X = self._vectorize(s, a)
        return self.Q.predict(X)[0]

    def _vectorize(self, s, a):
        X_s = self.state_vectorizer.transform([s])
        X_a = self.action_vectorizer.transform([a])
        return sparse.hstack([X_s, X_a]).tocsr()


class QSpider(BaseSpider):
    """
    Spider which uses Q-Learning to select links.
    It crawls a a list of URLs using adaptive algorithm
    and stores intermediate crawl graphs to ./checkpoints folder.

    Example::

        scrapy crawl q -a seeds_url=./urls.csv -L INFO

    """
    name = 'q'
    custom_settings = {
        'DEPTH_LIMIT': 5,
        # 'DEPTH_PRIORITY': 1,
        # 'CONCURRENT_REQUESTS':
    }

    # Crawler arguments
    replay_N = 0 # how many links to take for experience replay
    epsilon = 0  # probability of choosing a random link instead of
                 # the the most promising
    gamma = 0.5  # discounting factor

    # intervals for periodic tasks
    stats_interval = 10
    checkpoint_interval = 60*10
    update_link_scores_interval = 30

    # autogenerated crawl name
    crawl_id = "q" + str(datetime.datetime.now())

    ALLOWED_ARGUMENTS = BaseSpider.ALLOWED_ARGUMENTS | {
        'replay_N',
        'epsilon',
        'gamma',
        'crawl_id',
    }

    # crawl graph
    G = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.replay_N = int(self.replay_N)
        self.epsilon = float(self.epsilon)
        self.gamma = float(self.gamma)

        self.params = {
            'crawl_id': self.crawl_id,
            'replay_N': self.replay_N,
            'epsilon': self.epsilon,
            'gamma': self.gamma,
            # 'custom_settings': self.custom_settings,
        }

        self.logger.info("CRAWL {}".format(self.params))

        self.G = nx.DiGraph(name='Crawl Graph')
        self.node_ids = itertools.count()

        self.seen_urls = set()
        # self._replay_node_ids = set()
        self._scores_recalculated_at = 0
        self.domain_scores = MaxScores(available_form_types())

        self.log_task = LoopingCall(self.print_stats)
        self.log_task.start(self.stats_interval, now=False)
        self.checkpoint_task = LoopingCall(self.checkpoint)
        self.checkpoint_task.start(self.checkpoint_interval, now=False)

        self.rl_model = TDRegressor(gamma=self.gamma)
        self.update_link_scores_task = LoopingCall(self.update_request_priorities)
        self.update_link_scores_task.start(self.update_link_scores_interval, now=False)

        ensure_folder_exists(self._data_path(''))
        with open(self._data_path('info.json'), 'w') as f:
            json.dump(self.params, f)

        self.logger.info("Crawl {} started".format(self.crawl_id))

    def get_scheduler_queue(self):
        """
        This method is called by scheduler to create a new queue.
        """
        def new_queue(domain):
            return RequestsPriorityQueue(fifo=True)
        return BalancedPriorityQueue(queue_factory=new_queue, eps=self.epsilon)

    def parse(self, response):
        """ Main spider entry point - parse the response """
        self.increase_response_count()
        node_id = self.update_response_node(response)

        if self.G.node[node_id]['ok']:
            # don't send requests from failed responses
            yield from self.generate_out_nodes(response, node_id)

        self.learn_from_response(node_id, response)

        # TODO:
        # self.update_classifiers_bootstrapped(node_id)

    def learn_from_response(self, node_id, response):
        """ Learn from response """
        domain = get_response_domain(response)
        observed_scores = self.store_observed_scores(node_id, response)
        s_t = self.get_domain_state(domain).copy()
        self.update_domain_state(node_id, response)
        s_t1 = self.get_domain_state(domain)

        r_t1 = sum(max(v, 0.0) for k, v in dict_subtract(observed_scores, s_t).items())
        print("REWARD: {:0.2f}  {}".format(r_t1, response.url))

        a_t = self._get_action(node_id)
        if not a_t:
            return

        if r_t1 > 0.1:
            self.update_domain_request_priorities(domain)

        a_t1 = None
        next_req = self.scheduler_queue.get_domain_queue(domain).next_request
        if next_req:
            next_node_id = next_req.meta.get('node_id')
            if next_node_id is not None:
                a_t1 = self._get_action(next_node_id)

        self.rl_model.update(s_t, a_t, r_t1, s_t1, a_t1)

    def get_request_priority(self, domain, node_id):
        s = self.get_domain_state(domain)
        a = self._get_action(node_id)
        return int(self.rl_model.predict(s, a) * FLOAT_PRIORITY_MULTIPLIER)

    def _get_action(self, node_id):
        incoming_link_dicts = list(self._iter_incoming_link_dicts(node_id))

        if not incoming_link_dicts:
            return  # don't learn from seed URLs. XXX: should we?

        # Because of the way we crawl there is always either 0 or 1
        # incoming links.
        return incoming_link_dicts[0]

    def update_response_node(self, response):
        """
        Update crawl graph with information about the received response.
        Return node_id of the node which corresponds to this response.
        """
        node_id = response.meta.get('node_id')

        # 1. Handle responses for seed requests (they don't yet have node_id)
        is_seed_url = node_id is None
        if is_seed_url:
            node_id = next(self.node_ids)

        # 2. Update node with observed information
        ok = response.status == 200 and hasattr(response, 'text')

        self.G.add_node(
            node_id,
            url=response.url,
            visited=True,
            ok=ok,
            response_id=self.response_count,
        )

        # if not is_seed_url:
        #     # don't add initial nodes to replay because there is
        #     # no incoming links for such nodes
        #     self._replay_node_ids.add(node_id)

        return node_id

    def store_observed_scores(self, node_id, response):
        """
        Store observed information about node in the crawl graph.
        """
        # extract forms from response, classify them and get scores
        node = self.G.node[node_id]
        ok = node['ok']
        if ok:
            observed_scores = response_max_scores(response)
        else:
            observed_scores = get_constant_scores(0.0)
        self.G.add_node(node_id, scores=observed_scores)
        return observed_scores

    def get_domain_state(self, domain):
        return self.domain_scores[domain]

    def update_domain_state(self, node_id, response):
        node = self.G.node[node_id]
        domain = get_response_domain(response)
        scores = node['scores']
        if not scores:
            return
        self.domain_scores.update(domain, scores)
        # self.scheduler_queue.update_observed_scores(response, scores)

    def on_offdomain_request_dropped(self, request):
        super().on_offdomain_request_dropped(request)

        node_id = request.meta.get('node_id')
        if not node_id:
            self.logger.warn("Request without node_id dropped: {}".format(request))
            return

        self.G.add_node(
            node_id,
            visited=True,
            ok=False,
            scores=get_constant_scores(0.0),   # FIXME: it shouldn't be here?
            response_id=self.response_count,
        )
        # self._replay_node_ids.add(node_id)

    @property
    def scheduler_queue(self):
        return self.crawler.engine.slot.scheduler.queue

    def update_request_priorities(self, min_response_count=None):
        """ Update priorities of all requests in a frontier """
        if min_response_count is None:
            min_response_count = max(500, len(self.domain_scores))

        if min_response_count:
            interval = self.response_count - self._scores_recalculated_at
            if interval <= min_response_count:
                self.logger.info(
                    "Fewer than {} classifier updates ({}); not re-classifying links.".format(
                    min_response_count, interval
                ))
                return

        self.logger.info("Updating request priorities...")

        for domain in self.scheduler_queue.get_domains():
            self.update_domain_request_priorities(domain)

        self.logger.info("Updating request priorities: done.")
        self._scores_recalculated_at = self.response_count

    def update_domain_request_priorities(self, domain):
        """ Update all request priorities for a single domain """
        def _get_priority(request):
            node_id = request.meta.get('node_id')
            if not node_id:
                return request.priority
            return self.get_request_priority(domain, node_id)

        queue = self.scheduler_queue.get_domain_queue(domain)
        queue.update_all_priorities(_get_priority)

    def generate_out_nodes(self, response, this_node_id):
        """
        Extract links from the response and add nodes and edges to crawl graph.
        Returns an iterator of scrapy.Request objects.
        """

        # Extract in-domain links and their features
        domain = get_response_domain(response)
        links = list(self.iter_link_dicts(response, domain))
        random.shuffle(links)

        # Generate nodes, edges and requests based on link information
        for link in links:
            url = link['url']

            # generate nodes and edges
            node_id = next(self.node_ids)
            self.G.add_node(
                node_id,
                url=url,
                visited=False,
                ok=None,
                # scores=None,
                response_id=None,
            )
            self.G.add_edge(this_node_id, node_id, link=link)

            priority = self.get_request_priority(domain, node_id)

            # generate Scrapy request
            request = scrapy.Request(url, meta={
                'handle_httpstatus_list': [403, 404, 500],
                'node_id': node_id,
            }, priority=priority)
            set_request_domain(request, domain)
            yield request

    def _iter_incoming_link_dicts(self, node_id):
        for prev_id in self.G.predecessors_iter(node_id):
            yield self.G.edge[prev_id][node_id]['link']

    def print_stats(self):
        active_downloads = len(self.crawler.engine.downloader.active)
        self.logger.info("Active downloads: {}".format(active_downloads))
        msg = "Crawl graph: {} nodes ({} visited), {} edges, {} domains".format(
            self.G.number_of_nodes(),
            self.response_count,
            self.G.number_of_edges(),
            len(self.domain_scores)
        )
        self.logger.info(msg)

        scores_sum = sorted(self.domain_scores.sum().items())
        scores_avg = sorted(self.domain_scores.avg().items())
        reward_lines = [
            "{:8.1f}   {:0.4f}   {}".format(tot, avg, k)
            for ((k, tot), (k, avg)) in zip(scores_sum, scores_avg)
        ]
        msg = '\n'.join(reward_lines)
        self.logger.info("Reward (total / average): \n{}".format(msg))
        self.logger.info("Total reward: {}".format(sum(s for k, s in scores_sum)))

    def checkpoint(self):
        """
        Save current crawl state, which can be analyzed while
        the crawl is still going.
        """
        ts = int(time.time())
        graph_filename = 'crawl-{}.pickle.gz'.format(ts)
        # clf_filename = 'classifiers-{}.joblib'.format(ts)
        self.save_crawl_graph(graph_filename)
        # self.save_classifiers(clf_filename)

    def save_crawl_graph(self, path):
        self.logger.info("Saving crawl graph...")
        nx.write_gpickle(self.G, self._data_path(path))
        self.logger.info("Crawl graph saved")

    # def save_classifiers(self, path):
    #     self.logger.info("Saving classifiers...")
    #     pipe = {
    #         'vec': self.link_vectorizer,
    #         'clf': self.link_classifiers,
    #     }
    #     joblib.dump(pipe, self._data_path(path), compress=3)
    #     self.logger.info("Classifiers saved")

    def _data_path(self, path):
        return os.path.join('checkpoints', self.crawl_id, path)

    def closed(self, reason):
        """ Save crawl graph to a file when spider is closed """
        tasks = [
            self.log_task,
            self.checkpoint_task,
            self.update_link_scores_task
        ]
        for task in tasks:
            if task.running:
                task.stop()
        # self.save_classifiers('classifiers.joblib')
        self.save_crawl_graph('crawl.pickle.gz')
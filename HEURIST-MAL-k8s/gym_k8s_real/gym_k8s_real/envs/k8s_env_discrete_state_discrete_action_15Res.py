import datetime
import math
import random
import time
from pprint import pprint

import numpy as np
import requests
from gym import spaces
from gym.envs.toy_text import discrete
from kubernetes import client, config

twenty = 0
forty = 1
sixty = 2
eighty = 3
hundred = 4
hundred_fifty = 5
two_hundred = 6


class K8sEnvDiscreteStateDiscreteAction15Rres(discrete.DiscreteEnv):
    metadata = {
        'render.modes': ['human']
    }

    def __init__(self, timestep_duration, app_names, sla_latency,
            sla_host, sla_latency_metric_name, cpu_thresh_init=None):
        config.load_kube_config()

        # General variables defining the environment
        # Get following info from k8s
        num_states = 1225
        num_actions = 3
        P = {
            state: {
                action: [] for action in range(num_actions)
            } for state in range(num_states)
        }
        initial_state_distrib = np.zeros(num_states)
        self.cpu_thresh_init = cpu_thresh_init
        self.done = False
        self.MAX_PODS = 10
        self.MIN_PODS = 1
        self.timestep_duration = timestep_duration
        self.app_names = app_names
        self.sla_latency = float(sla_latency)
        self.sla_host = sla_host
        self.sla_latency_metric_name = sla_latency_metric_name

        self.observation_space = spaces.Tuple((
            spaces.Discrete(7),  # pod_cpu_percent
            spaces.Discrete(5),  # hpa_cpu_threshold
            spaces.Discrete(5),  # pod_replicas_percent
            spaces.Discrete(7)   # latency
        ))

        self.action_space = spaces.Discrete(3)
        discrete.DiscreteEnv.__init__(
            self, num_states, num_actions, P, initial_state_distrib
        )

    def step(self, action):
        """
        Returns
        -------
        encoded_observation, reward, done, dt_dict : tuple
            encoded_observation : int
                discretized environment observation encoded in an integer
            real_observation: list
                list of observations that contains the current cpu utilization,
                the hpa cpu threshold, the current number of pods and the
                latency
            reward : float
                amount of reward achieved by the previous action. The scale
                varies between environments, but the goal is always to increase
                your total reward.
            done : boolean
                boolean value of whether training is done or not. Becomes True
                when errors occur.
            dt_dict : Dict
                dictionary of formatted date and time.
        """
        encoded_observation, now_observation = self._get_state()
        if action == 0 and now_observation[1] <= 20:
            return now_observation, 0, self.done, encoded_observation
        if action == 2 and now_observation[1] >= 80:
            return now_observation, 0, self.done, encoded_observation
#         if action == 1:
#             reward = self._get_reward(now_observation)
#             return now_observation, reward, self.done, encoded_observation
        
        self._take_action(action)  # Create HPA
        wait_time = self.timestep_duration * 60
        time.sleep(wait_time)  # Wait timestep_duration minutes for the changes to take place

        encoded_observation, real_observation = self._get_state()
        reward = self._get_reward(real_observation)

        if -1 in self.decode(encoded_observation):
            self.done = True
            reward = 0

        now = datetime.datetime.now()
        dt_string = now.strftime('%d/%m/%Y %H:%M:%S')
        dt_dict = {
            'datetime': dt_string
        }

        return real_observation, reward, self.done, encoded_observation
    
    def random_step(self, threshold):
        """
        Returns
        -------
        encoded_observation, reward, done, dt_dict : tuple
            encoded_observation : int
                discretized environment observation encoded in an integer
            real_observation: list
                list of observations that contains the current cpu utilization,
                the hpa cpu threshold, the current number of pods and the
                latency
            reward : float
                amount of reward achieved by the previous action. The scale
                varies between environments, but the goal is always to increase
                your total reward.
            done : boolean
                boolean value of whether training is done or not. Becomes True
                when errors occur.
            dt_dict : Dict
                dictionary of formatted date and time.
        """
        
        self._create_hpa(threshold)  # Create HPA
        wait_time = self.timestep_duration * 60
        time.sleep(wait_time)  # Wait timestep_duration minutes for the changes to take place

        encoded_observation, real_observation = self._get_state()
        

        if -1 in self.decode(encoded_observation):
            self.done = True
            reward = 0

        now = datetime.datetime.now()
        dt_string = now.strftime('%d/%m/%Y %H:%M:%S')
        dt_dict = {
            'datetime': dt_string
        }

        return real_observation, 0, self.done, encoded_observation

    def reset(self):
        possible_thresholds = list(
            range(20, 101, 20)
        )
        
        if self.cpu_thresh_init is not None:
            cpu_thresh = self.cpu_tresh_init
        else:
            cpu_thresh = random.choice(possible_thresholds)

        self._create_hpa(cpu_thresh)
        return self._get_state()

    def render(self, mode='human'):
        return None

    def close(self):
        pass

    def _take_action(self, action):
        if action == 1:
            return

        (hpa_error,
         pod_cpu_current_util,
         pod_cpu_threshold,
         current_replicas) = self._get_existing_app_hpa()

        if hpa_error == 1:
            self.done = True
        else:
            self.done = False

        if pod_cpu_threshold != 0:
            # Delete the hpa
            v2 = client.AutoscalingV2beta2Api()
            api_response = v2.delete_namespaced_horizontal_pod_autoscaler(
                name=self.app_name, namespace='default', pretty='true'
            )

        # Adjust the threshold
        new_cpu_hpa_threshold = pod_cpu_threshold

        if action == 0 and pod_cpu_threshold > 20:
            new_cpu_hpa_threshold -= 20

        if action == 2 and pod_cpu_threshold < 80:
            new_cpu_hpa_threshold += 40

        self._create_hpa(new_cpu_hpa_threshold)
    
    # expose to external 
    def get_state(self):
        # Get metrics from metrics-server API
        (hpa_error,
         pod_cpu_current_util,
         pod_cpu_threshold,
         current_replicas) = self._get_existing_app_hpa()

        # pod_throughput = self._query_prometheus(self.prometheus_throughput_metric_name)
        pod_throughput = self._query_latency(self.prometheus_throughput_metric_name)
        
        if math.isnan(pod_throughput):
            pod_throughput = self.sla_throughput

        real_observation = [
            pod_cpu_current_util,
            pod_cpu_threshold,
            current_replicas,
            pod_throughput
        ]

        current_replicas_percent = 100 * current_replicas / self.MAX_PODS
        pod_throughput_percent = 100 * pod_throughput / self.sla_throughput

        # Adjust the threshold to match the discrete value
        # ranges for {0, 1, 2, 3, 4}
        real_observation_percent = [
            pod_cpu_current_util,
            pod_cpu_threshold - 20,
            current_replicas_percent,
            pod_throughput_percent
        ]

        discretized_observation = [self._get_discrete(ob) for ob in real_observation_percent]
        encoded_observation = self.encode(
            discretized_observation[0],
            discretized_observation[1],
            discretized_observation[2],
            discretized_observation[3]
        )

        return encoded_observation, real_observation

    def _get_state(self):
        # Get metrics from metrics-server API
        (hpa_error,
         pod_cpu_current_util,
         pod_cpu_threshold,
         current_replicas) = self._get_existing_app_hpa()

        # pod_throughput = self._query_prometheus(self.prometheus_throughput_metric_name)
        pod_throughput = self._query_latency(self.prometheus_throughput_metric_name)
        
        if math.isnan(pod_throughput):
            pod_throughput = self.sla_throughput

        real_observation = [
            pod_cpu_current_util,
            pod_cpu_threshold,
            current_replicas,
            pod_throughput
        ]

        current_replicas_percent = 100 * current_replicas / self.MAX_PODS
        pod_throughput_percent = 100 * pod_throughput / self.sla_throughput

        # Adjust the threshold to match the discrete value
        # ranges for {0, 1, 2, 3, 4}
        real_observation_percent = [
            pod_cpu_current_util,
            pod_cpu_threshold - 20,
            current_replicas_percent,
            pod_throughput_percent
        ]

        discretized_observation = [self._get_discrete(ob) for ob in real_observation_percent]
        encoded_observation = self.encode(
            discretized_observation[0],
            discretized_observation[1],
            discretized_observation[2],
            discretized_observation[3]
        )

        return encoded_observation, real_observation

    def _get_reward(self, real_observation):
        """
        Calculate reward value: The environment receives the current values of
        pod_number and cpu/memory metric values that correspond to the current
        state of the system s. The reward value r is calculated based on two
        criteria:
        (i)  the amount of resources acquired,
             which directly determines the cost
        (ii) the number of pods needed to support the received load.
        """

        (pod_cpu_current_util,
         pod_cpu_threshold,
         pod_number,
         pod_throughput) = real_observation

        reward_min = 0
        reward_max = 25
        reward = 0

        pod_weight = 1.5
        throughput_weight = 1

        d = 5.0  # this is a hyperparameter of the reward function

        #计算throughput的reward
        # throughput_ratio = self.sla_throughput / (pod_throughput+1)

        #用latency计算reward
        throughput_ratio = pod_throughput / self.sla_throughput

        if pod_number == 1 and throughput_ratio <= 1:
            return reward_max
        elif throughput_ratio > 5:
            return reward_min

        pod_reward = -10 / (self.MAX_PODS - 1) * pod_number \
            + 10 * self.MAX_PODS / (self.MAX_PODS - 1)
        reward += pod_weight * pod_reward

        #用throughput计算reward
        # throughout_ref_value = 1
        # if throughput_ratio < throughout_ref_value:
        #     throughput_reward = 10 * pow(math.e, -0.3 * d * throughput_ratio)
        # else:
        #     throughput_reward = 10 * pow(math.e, -10 * d * throughput_ratio)
        # reward += throughput_weight * throughput_reward

        #用latency计算reward
        throughout_ref_value = 1
        if throughput_ratio < throughout_ref_value:
            throughput_reward = 10 * pow(math.e, -0.3 * d * throughput_ratio)
        else:
            throughput_reward = 10 * pow(math.e, -10 * d * (throughput_ratio - throughout_ref_value))
        reward += throughput_weight * throughput_reward

        return reward

    #get average_cpu for all pods with app_name on cluster with cluster
    def _get_average_cpu(self, app_name, cluster):
        aver_cpu = 0
        #To-do: obtain from metric server and calculate
        return aver_cpu

    #get number of active pods with app_name on cluster with cluster
    def _get_pods_num(self, app_name, cluster):
        num = 0
        #To-do: obtain from metric server
        return num

    #get the index of allocated clusters with active pod
    def _get_placement(self, app_name):
        clusters = []
        #To-do: use _get_pods_num to calculate number on each cluster and output
        return clusters

    #get latency metric
    def _query_latency(self, query_name):
        latency_endpoint = self.sla_host + query_name
        response = requests.get(
            latency_endpoint
        )
        results = response.json()
        if results:
            return results
        else:
            return float('NaN')

    #deploy 'number' pods with 'app_name' on cluster 'cluster'
    def _deploy_pods(self, app_name, number, cluster):

        return

    def _get_discrete(self, number):
        """
        Get a number and return the discrete level it belongs to
        """
        number = round(number, 0)

        if number in range(-1, 20):
            return twenty
        elif number in range(20, 40):
            return forty
        elif number in range(40,60):
            return sixty
        elif number in range(60, 80):
            return eighty
        elif number in range(80, 101):
            return hundred
        elif number in range(101, 150):
            return hundred_fifty
        elif number in range(150, 200) or number >= 200:
            return two_hundred
        else:
            return -1

    def encode(self, cpu_util, threshold, pods, throughput):
        """
        Encode the discrete observation values in one single number.
        CPU utilization and latency can take values in {0, 1, 2, 3, 4, 5, 6}
        CPU threshold and pod utilization can take values in {0, 1, 2, 3, 4}
        """
        assert 0 <= cpu_util < 7
        assert 0 <= threshold < 5
        assert 0 <= pods < 5
        assert 0 <= throughput < 7

        i = cpu_util
        i *= 5
        i += threshold
        i *= 5
        i += pods
        i *= 7
        i += throughput
        return i

    def decode(self, i):
        out = []
        out.append(i % 7)
        i //= 7
        out.append(i % 5)
        i //= 5
        out.append(i % 5)
        i //= 5
        out.append(i)
        return reversed(out)

    def _query_prometheus(self, query_name):
        prometheus_endpoint = '{}/api/v1/query'.format(self.prometheus_host)
        response = requests.get(
            prometheus_endpoint, params={
                'query': query_name
            }
        )

        results = response.json()['data']['result']
        if len(results) > 0:
            return float(results[0]['value'][1])
        else:
            return float('NaN')

    

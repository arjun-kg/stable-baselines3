import copy
from enum import Enum
from typing import Union, Optional

import numpy as np
import torch as th
from gym import spaces
from stable_baselines3.common.buffers import BaseBuffer
from stable_baselines3.common.type_aliases import ReplayBufferSamples
from stable_baselines3.common.vec_env import VecNormalize


class GoalSelectionStrategy(Enum):
    """
    The strategies for selecting new goals when
    creating artificial transitions.
    """
    # Select a goal that was achieved
    # after the current step, in the same episode
    FUTURE = 0
    # Select the goal that was achieved
    # at the end of the episode
    FINAL = 1
    # Select a goal that was achieved in the episode
    EPISODE = 2
    # Select a goal that was achieved
    # at some point in the training procedure
    # (and that is present in the replay buffer)
    RANDOM = 3


# For convenience
# that way, we can use string to select a strategy
KEY_TO_GOAL_STRATEGY = {
    'future': GoalSelectionStrategy.FUTURE,
    'final': GoalSelectionStrategy.FINAL,
    'episode': GoalSelectionStrategy.EPISODE,
    'random': GoalSelectionStrategy.RANDOM
}


class HindsightExperienceReplayBuffer(BaseBuffer):
    """
    Replay buffer used for HER

    :param buffer_size: (int) Max number of element in the buffer
    :param observation_space: (spaces.Space) Observation space
    :param action_space: (spaces.Space) Action space
    :param max_episode_len: int
    :param wrapped_env Env wrapped with HER wrapper
    :param device: (th.device)
    :param n_envs: (int) Number of parallel environments
    :param add_her_while_sampling: (bool) Whether to add HER transitions while sampling or while storing
    :param goal_selection_strategy (string)
    :param n_sampled_goal (int)
    """

    def __init__(self,
                 buffer_size: int,
                 observation_space: spaces.Space,
                 action_space: spaces.Space,
                 max_episode_len: int,
                 wrapped_env,
                 device: Union[th.device, str] = 'cpu',
                 n_envs: int = 1,
                 add_her_while_sampling: bool = False,
                 goal_selection_strategy: str = 'future',
                 n_sampled_goal: int = 4,
                 ):
        super(HindsightExperienceReplayBuffer, self).__init__(buffer_size, observation_space,
                                                              action_space, device, n_envs=n_envs)

        assert n_envs == 1, "Replay buffer only support single environment for now"

        # Assume all episodes have same lengths for now
        self.max_episode_len = max_episode_len
        self.add_her_while_sampling = add_her_while_sampling
        self.goal_selection_strategy = KEY_TO_GOAL_STRATEGY[goal_selection_strategy]
        self.n_sampled_goal = n_sampled_goal
        self.env = wrapped_env

        if not self.add_her_while_sampling:
            batch_shape = (self.buffer_size,)
            self.observations = np.zeros((*batch_shape, self.n_envs,) + self.obs_shape, dtype=np.float32)
            self.next_observations = np.zeros((*batch_shape, self.n_envs,) + self.obs_shape, dtype=np.float32)
        else:
            batch_shape = (self.buffer_size // self.max_episode_len, self.max_episode_len)
            self.observations = np.zeros((self.buffer_size // self.max_episode_len, self.max_episode_len + 1,
                                          self.n_envs,) + self.obs_shape, dtype=np.float32)
            # No need next_observations variable if transitions are being stored as episodes

            self.n_episode_steps = np.zeros(self.buffer_size // self.max_episode_len)
            # for variable sized episodes, episode will be stored with zero padding
            # This variable keeps track of where the episode actually ended
            # Large part of buffer is being wasted if most episodes don't run to max_episode_len
            # but this is a quick fix for now, variable len not possible with numpy arrays

        self.actions = np.zeros((*batch_shape, self.n_envs, self.action_dim), dtype=np.float32)
        self.rewards = np.zeros((*batch_shape, self.n_envs), dtype=np.float32)
        self.dones = np.zeros((*batch_shape, self.n_envs), dtype=np.float32)
        self.pos = 0
        self.episode_transitions = []

    def add(self, obs: np.ndarray,
            next_obs: np.ndarray,
            action: float,
            reward: float,
            done: bool):
        """
        add a new transition to the buffer

        :param obs: (np.ndarray) the last observation
        :param next_obs: (np.ndarray) the new observation
        :param action: ([float]) the action
        :param reward: (float) the reward of the transition
        :param done: (bool) is the episode done
        """
        # Update current episode buffer
        self.episode_transitions.append((obs, next_obs, action, reward, done))
        if done:
            # Add transitions to buffer only when an episode is over
            self._store_episode()
            # Reset episode buffer
            self.episode_transitions = []

    def _get_samples(self,
                     batch_inds: np.ndarray,
                     env: Optional[VecNormalize] = None
                     ) -> ReplayBufferSamples:
        if not self.add_her_while_sampling:
            data = (self._normalize_obs(self.observations[batch_inds, 0, :], env),
                    self.actions[batch_inds, 0, :],
                    self._normalize_obs(self.next_observations[batch_inds, 0, :], env),
                    self.dones[batch_inds],
                    self._normalize_reward(self.rewards[batch_inds], env))
            return ReplayBufferSamples(*tuple(map(self.to_torch, data)))
        else:
            '''
            Sampling inspired by https://github.com/openai/baselines/blob/master/baselines/her/her_sampler.py
            '''

            # TODO: Implement modes other than future
            if self.goal_selection_strategy == GoalSelectionStrategy.FUTURE:
                future_p = 1 - (1. / (1 + self.n_sampled_goal))
            elif self.goal_selection_strategy == GoalSelectionStrategy.FINAL:
                raise NotImplementedError
            # future_t is always last timestep. Rest of code is same
            elif self.goal_selection_strategy == GoalSelectionStrategy.EPISODE:
                raise NotImplementedError
            # future_t is random value from 0 to last timestep
            elif self.goal_selection_strategy == GoalSelectionStrategy.RANDOM:
                raise NotImplementedError
            # sample second set of episode + timestep indices, use those ag's as dg's for the first set...
            else:
                raise ValueError("Invalid goal selection strategy,"
                                 "please use one of {}".format(list(GoalSelectionStrategy)))

            episode_inds = batch_inds  # renaming for better clarity
            max_timestep_inds = self.n_episode_steps[episode_inds]
            batch_size = len(episode_inds)
            timestep_inds = np.floor(np.random.uniform(max_timestep_inds)).astype(int)
            # randint does not support array, using np.uniform with floor instead

            # Select future time indexes proportional with probability future_p. These
            # will be used for HER replay by substituting in future goals.
            her_inds = np.where(np.random.uniform(size=batch_size) < future_p)
            future_offset = np.random.uniform(size=batch_size) * (max_timestep_inds - timestep_inds)
            future_offset = future_offset.astype(int)
            future_t = (timestep_inds + 1 + future_offset)[her_inds]

            # Replace goal with achieved goal but only for the previously-selected
            # HER transitions (as defined by her_indexes). For the other transitions,
            # keep the original goal.

            observations_dict = self.env.convert_obs_to_dict(self.observations[episode_inds])
            future_ag = observations_dict['achieved_goal'][her_inds, future_t, np.newaxis][0]
            observations_dict['desired_goal'][her_inds, :] = future_ag

            rewards = self.env.compute_reward(observations_dict['achieved_goal'],
                                              observations_dict['desired_goal'], None)[:, 1:].astype(np.float32)
            # Skip reward computed for initial states

            obs = self.env.convert_dict_to_obs(observations_dict)
            data = (self._normalize_obs(obs[np.arange(obs.shape[0]), timestep_inds][:, 0], env),
                    self.actions[episode_inds][np.arange(batch_size), timestep_inds][:, 0],
                    self._normalize_obs(obs[np.arange(obs.shape[0]), timestep_inds + 1][:, 0], env),
                    self.dones[episode_inds][np.arange(batch_size), timestep_inds],
                    self._normalize_reward(rewards[np.arange(batch_size), timestep_inds], env))

            return ReplayBufferSamples(*tuple(map(self.to_torch, data)))

    def sample(self,
               batch_size: int,
               env: Optional[VecNormalize] = None
               ):
        """
        :param batch_size: (int) Number of element to sample
        :param env: (Optional[VecNormalize]) associated gym VecEnv
            to normalize the observations/rewards when sampling
        :return: (Union[RolloutBufferSamples, ReplayBufferSamples])
        """
        full_idx = self.buffer_size // self.max_episode_len if self.add_her_while_sampling else self.buffer_size
        upper_bound = full_idx if self.full else self.pos
        batch_inds = np.random.randint(0, upper_bound, size=batch_size)
        return self._get_samples(batch_inds, env=env)

    def _store_episode(self):
        """
        Sample artificial goals and store transition of the current
        episode in the replay buffer. This method is called only after each end of episode.
        For second mode, only regular transitions are stored, HER transitions are created while sampling
        """
        if not self.add_her_while_sampling:
            # For each transition in the last episode,
            # create a set of artificial transitions
            for transition_idx, transition in enumerate(self.episode_transitions):

                obs, next_obs, action, reward, done = transition  # BEGIN CHECK
                self._add_transition(obs, next_obs, action, reward, done)

                # We cannot sample a goal from the future in the last step of an episode
                if (transition_idx == len(self.episode_transitions) - 1 and
                        self.goal_selection_strategy == GoalSelectionStrategy.FUTURE):
                    break

                # Sampled n goals per transition, where n is `n_sampled_goal`
                # this is called k in the paper
                sampled_goals = self._sample_achieved_goals(self.episode_transitions, transition_idx)  # CHECK
                # For each sampled goals, store a new transition
                for goal in sampled_goals:
                    # Copy transition to avoid modifying the original one
                    obs, next_obs, action, reward, done = copy.deepcopy(transition)

                    # Convert concatenated obs to dict, so we can update the goals
                    obs_dict, next_obs_dict = map(self.env.convert_obs_to_dict, (obs[0], next_obs[0]))

                    # Update the desired goal in the transition
                    obs_dict['desired_goal'] = goal
                    next_obs_dict['desired_goal'] = goal

                    # Update the reward according to the new desired goal
                    reward = self.env.compute_reward(next_obs_dict['achieved_goal'], goal, None)
                    # Can we use achieved_goal == desired_goal?
                    done = False

                    # Transform back to ndarrays
                    obs, next_obs = map(self.env.convert_dict_to_obs, (obs_dict, next_obs_dict))

                    # Add artificial transition to the replay buffer
                    self._add_transition(obs, next_obs, action, reward, done)
        else:

            episode_transitions_zipped = [np.array(item) for item in list(zip(*self.episode_transitions))]
            obs, next_obs, action, reward, done = episode_transitions_zipped
            self._add_transition(obs, next_obs, action, reward, done)

    def _add_transition(self, obs: np.ndarray,
                        next_obs: np.ndarray,
                        action: np.ndarray,
                        reward: Union[float, np.ndarray],
                        done: Union[bool, np.ndarray]):

        if not self.add_her_while_sampling:
            self.observations[self.pos] = np.array(obs).copy()
            self.next_observations[self.pos] = np.array(next_obs).copy()
            self.actions[self.pos] = np.array(action).copy()
            self.rewards[self.pos] = np.array(reward).copy()
            self.dones[self.pos] = np.array(done).copy()

            self.pos += 1
            if self.pos == self.buffer_size:
                self.full = True
                self.pos = 0
        else:
            if len(obs.shape) >= 2:
                len_episode = len(obs)
            else:
                raise Exception("obs needs to be an episode of observations")
            self.observations[self.pos, :len_episode + 1] = np.append(np.array(obs).copy(),
                                                                      np.array(next_obs[np.newaxis, -1].copy()), axis=0)
            self.actions[self.pos, :len_episode] = np.array(action).copy()
            self.rewards[self.pos, :len_episode] = np.array(reward).copy()
            self.dones[self.pos, :len_episode] = np.array(done).copy()

            self.n_episode_steps[self.pos] = len_episode

            self.pos += 1
            if self.pos == self.buffer_size // self.max_episode_len:
                self.full = True
                self.pos = 0

    def _sample_achieved_goals(self, episode_transitions: list,
                               transition_idx: int):
        """
        Sample a batch of achieved goals according to the sampling strategy.

        :param episode_transitions: ([tuple]) list of the transitions in the current episode
        :param transition_idx: (int) the transition to start sampling from
        :return: (np.ndarray) an achieved goal
        """
        return [
            self._sample_achieved_goal(episode_transitions, transition_idx)
            for _ in range(self.n_sampled_goal)
        ]

    def _sample_achieved_goal(self, episode_transitions: list,
                              transition_idx: int):
        """
        Sample an achieved goal according to the sampling strategy.

        :param episode_transitions: ([tuple]) a list of all the transitions in the current episode
        :param transition_idx: (int) the transition to start sampling from
        :return: (np.ndarray) an achieved goal
        """
        if self.goal_selection_strategy == GoalSelectionStrategy.FUTURE:
            # Sample a goal that was observed in the same episode after the current step
            selected_idx = np.random.choice(np.arange(transition_idx + 1, len(episode_transitions)))
            selected_transition = episode_transitions[selected_idx]
        elif self.goal_selection_strategy == GoalSelectionStrategy.FINAL:
            # Choose the goal achieved at the end of the episode
            selected_transition = episode_transitions[-1]
        elif self.goal_selection_strategy == GoalSelectionStrategy.EPISODE:
            # Random goal achieved during the episode
            selected_idx = np.random.choice(np.arange(len(episode_transitions)))
            selected_transition = episode_transitions[selected_idx]
        elif self.goal_selection_strategy == GoalSelectionStrategy.RANDOM:
            # Random goal achieved, from the entire replay buffer
            raise NotImplementedError  
        else:
            raise ValueError("Invalid goal selection strategy,"
                             "please use one of {}".format(list(GoalSelectionStrategy)))
        return self.env.convert_obs_to_dict(selected_transition[0][0])['achieved_goal']

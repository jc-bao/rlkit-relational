import abc
import pickle
import time
from collections import OrderedDict

import gtimer as gt
import numpy as np

from rlkit.core import eval_util, logger
from rlkit.data_management.env_replay_buffer import EnvReplayBuffer
from rlkit.data_management.path_builder import PathBuilder
from rlkit.policies.base import ExplorationPolicy
from rlkit.samplers.in_place import InPlacePathSampler
import rlkit.torch.pytorch_util as ptu
try:
    from mpi4py import MPI
except ImportError:
    MPI = None
import torch
from rlkit.torch.relational.relational_util import get_masks


class RLAlgorithm(metaclass=abc.ABCMeta):
    def __init__(
            self,
            env,
            exploration_policy: ExplorationPolicy,
            training_env=None,
            num_epochs=100,
            num_steps_per_epoch=10000,
            num_steps_per_eval=1000,
            num_updates_per_env_step=1,
            num_updates_per_epoch=None,
            batch_size=1024,
            max_path_length=1000,
            discount=0.99,
            replay_buffer_size=1000000,
            reward_scale=1,
            min_num_steps_before_training=None,
            render=False,
            save_replay_buffer=False,
            save_algorithm=False,
            save_environment=True,
            eval_sampler=None,
            eval_policy=None,
            replay_buffer=None,
            collection_mode='online',
            save_extra_data_interval=100000,
            num_gpus=1,
            num_epochs_per_eval=10,
            num_epochs_per_param_save=100,
            **kwargs
    ):
        """
        Base class for RL Algorithms

        :param env: Environment used to evaluate.
        :param exploration_policy: Policy used to explore
        :param training_env: Environment used by the algorithm. By default, a
        copy of `env` will be made for training, so that training and
        evaluation are completely independent.
        :param num_epochs:
        :param num_steps_per_epoch:
        :param num_steps_per_eval:
        :param num_updates_per_env_step: Used by online training mode.
        :param num_updates_per_epoch: Used by batch training mode.
        :param batch_size:
        :param max_path_length:
        :param discount:
        :param replay_buffer_size:
        :param reward_scale:
        :param min_num_steps_before_training:
        :param render:
        :param save_replay_buffer:
        :param save_algorithm:
        :param save_environment:
        :param eval_sampler:
        :param eval_policy: Policy to evaluate with.
        :param replay_buffer:
        :param collection_mode: String determining how training happens
         - 'online': Train after every step taken in the environment.
         - 'batch': Train after every epoch.
        """
        assert collection_mode in ['online', 'batch']
        if collection_mode == 'batch':
            assert num_updates_per_epoch is not None

        self.training_env = training_env or pickle.loads(pickle.dumps(env))
        self.exploration_policy = exploration_policy
        self.num_epochs = num_epochs
        self.num_env_steps_per_epoch = num_steps_per_epoch
        self.num_steps_per_eval = num_steps_per_eval
        if collection_mode == 'online':
            self.num_updates_per_train_call = num_updates_per_env_step
        else:
            self.num_updates_per_train_call = num_updates_per_epoch
        self.batch_size = batch_size
        self.max_path_length = max_path_length
        self.discount = discount
        self.replay_buffer_size = replay_buffer_size
        self.reward_scale = reward_scale
        self.render = render
        self.collection_mode = collection_mode
        self.save_replay_buffer = save_replay_buffer
        self.save_algorithm = save_algorithm
        self.save_environment = save_environment
        if min_num_steps_before_training is None:
            min_num_steps_before_training = self.num_env_steps_per_epoch
        self.min_num_steps_before_training = min_num_steps_before_training
        if eval_sampler is None:
            if eval_policy is None:
                eval_policy = exploration_policy
            eval_sampler = InPlacePathSampler(
                env=env,
                policy=eval_policy,
                max_samples=self.num_steps_per_eval + self.max_path_length,
                max_path_length=self.max_path_length,
            )
        self.eval_policy = eval_policy
        self.eval_sampler = eval_sampler
        self.eval_statistics = OrderedDict()
        self.need_to_update_eval_statistics = True

        self.action_space = env.action_space
        self.obs_space = env.observation_space
        self.env = env
        if replay_buffer is None:
            replay_buffer = EnvReplayBuffer(
                self.replay_buffer_size,
                self.env,
            )
        self.replay_buffer = replay_buffer

        self._n_env_steps_total = 0
        self._n_train_steps_total = 0
        self._n_rollouts_total = 0
        self._do_train_time = 0
        self._epoch_start_time = None
        self._algo_start_time = None
        self._old_table_keys = None
        self._current_path_builder = PathBuilder()
        self._exploration_paths = []
        self.post_epoch_funcs = []
        self.save_extra_data_interval = save_extra_data_interval

        # MPI stuff
        if MPI and ptu.get_mode():
            self.gpu_id = MPI.COMM_WORLD.Get_rank()%num_gpus

        self.num_epochs_per_eval = num_epochs_per_eval
        assert num_epochs_per_param_save % num_epochs_per_eval == 0
        self.num_epochs_per_param_save = num_epochs_per_param_save

        import collections
        # self.reward_buffer = collections.deque([-2*10], 10)

    def train(self, start_epoch=0):
        self.pretrain()
        if start_epoch == 0 and MPI and MPI.COMM_WORLD.Get_rank() == 0:
            params = self.get_epoch_snapshot(-1)
            logger.save_itr_params(-1, params)
        self.training_mode(False)
        self._n_env_steps_total = start_epoch * self.num_env_steps_per_epoch
        gt.reset()
        gt.set_def_unique(False)
        if self.collection_mode == 'online':
            self.train_online(start_epoch=start_epoch)
        elif self.collection_mode == 'batch': # use this way
            self.train_batch(start_epoch=start_epoch)
        else:
            raise TypeError("Invalid collection_mode: {}".format(
                self.collection_mode
            ))

    def pretrain(self):
        pass

    def train_online(self, start_epoch=0):
        self._current_path_builder = PathBuilder()
        for epoch in gt.timed_for(
                range(start_epoch, self.num_epochs),
                save_itrs=True,
        ):
            self._start_epoch(epoch)
            set_to_train_mode(self.training_env)
            observation = self._start_new_rollout()
            for _ in range(self.num_env_steps_per_epoch):
                observation = self._take_step_in_env(observation)
                gt.stamp('sample')

                self._try_to_train()
                gt.stamp('train')

            set_to_eval_mode(self.env)
            self._try_to_eval(epoch)
            gt.stamp('eval')
            self._end_epoch(epoch)

    def train_batch(self, start_epoch):
        self._current_path_builder = PathBuilder()
        for epoch in gt.timed_for(
                range(start_epoch, self.num_epochs),
                save_itrs=True,
        ):
            self._start_epoch(epoch)
            set_to_train_mode(self.training_env)
            observation = self._start_new_rollout()
            # This implementation is rather naive. If you want to (e.g.)
            # parallelize data collection, this would be the place to do it.50)
            for _ in range(self.num_env_steps_per_epoch):
                observation = self._take_step_in_env(observation)
            gt.stamp('sample')

            # self.qf1_optimizer.reinit_flat_operators() #TODO what is this
            self._try_to_train()
            gt.stamp('train')

            set_to_eval_mode(self.env)
            if epoch % self.num_epochs_per_eval == 0:
                self._try_to_eval(epoch)
                gt.stamp('eval')
            self._end_epoch(epoch)

    def _take_step_in_env(self, observation):
        action, agent_info = self._get_action_and_info(
            observation,
        )

        # TODO: remove
        # self.qf1.pooler.current_time_step += 1
        # self.qf2.pooler.current_time_step += 1
        # self.vf.pooler.current_time_step += 1
        # self.qf1.pooler.max_time_horizon = 50 * 2
        # self.qf2.pooler.max_time_horizon = 50 * 2
        # self.vf.pooler.max_time_horizon = 50 * 2

        if self.render:
            self.training_env.render()
        next_ob, raw_reward, terminal, env_info = (
            self.training_env.step(action)
        )
        # self.reward_buffer.append(raw_reward)
        # if sum(self.reward_buffer) >= 0 and self.policy.selection_attention.hard_block == 0:
        #     self.policy.selection_attention.hard_block = 1
            # self.qf1.pooler.selection_attention.hard_block = 1

        self._n_env_steps_total += 1
        reward = raw_reward * self.reward_scale
        terminal = np.array([terminal])
        reward = np.array([reward])
        self._handle_step(
            observation,
            action,
            reward,
            next_ob,
            terminal,
            agent_info=agent_info,
            env_info=env_info,
            mask=get_masks(self.training_env.unwrapped.num_blocks, self.replay_buffer.max_num_blocks, 1)
        )
        # print(F"cpb len {len(self._current_path_builder)}")
        # print(F"terminal {terminal}")
        if terminal or len(self._current_path_builder) >= self.max_path_length:
            self._handle_rollout_ending()
            new_observation = self._start_new_rollout()
        else:
            new_observation = next_ob
        return new_observation

    def _try_to_train(self):
        # assert self.alpha_optimizer.param_groups[0]['params'][0]
        if ptu.get_mode() == "gpu_opt":
            ptu.set_device(device_id=self.gpu_id, device_type="gpu")
            self.to(device=torch.device(F"cuda:{self.gpu_id}"))
            # assert self.alpha_optimizer.m.device.type == "cuda"

        # assert self.alpha_optimizer.m.device.type == "cuda"
        if self._can_train():
            self.training_mode(True)
            # assert self.alpha_optimizer.m.device.type == "cuda"
            for i in range(self.num_updates_per_train_call):
                self._do_training()
                # assert self.alpha_optimizer.m.device.type == "cuda"
                self._n_train_steps_total += 1
            self.training_mode(False)
        if ptu.get_mode() == "gpu_opt":
            ptu.set_device(device_type="cpu")
            self.to(device=torch.device("cpu"))

    def _try_to_eval(self, epoch, eval_paths=None):
        if MPI and MPI.COMM_WORLD.Get_rank() == 0:
            if epoch % self.save_extra_data_interval == 0:
                logger.save_extra_data(self.get_extra_data_to_save(epoch))

            if epoch % self.num_epochs_per_param_save == 0:
                print("Attemping itr param save...")
                params = self.get_epoch_snapshot(epoch)
                logger.save_itr_params(epoch, params)
                print(F"Itr{epoch} param saved!")

        if self._can_evaluate():
            self.evaluate(epoch, eval_paths=eval_paths)

            logger.record_tabular(
                "Number of train steps total",
                self._n_train_steps_total,
            )
            logger.record_tabular(
                "Number of env steps total",
                self._n_env_steps_total,
            )
            logger.record_tabular(
                "Number of rollouts total",
                self._n_rollouts_total,
            )

            times_itrs = gt.get_times().stamps.itrs
            # train_time = times_itrs['train'][-1]
            training_loops = ['get_batch', 'update_normalizer', 'forward', 'compute_losses', 'qf1_loop', "policy_loss_forward", 'policy_loop', 'vf_loop']
            train_time = sum(times_itrs[loop][-1] for loop in times_itrs.keys())

            sample_time = times_itrs['sample'][-1]

            if epoch > 0:
                eval_time = times_itrs['eval'][-1]
            else:
                times_itrs['eval'] = [0] # Need to do this so we can do line 343, the list comprehension
                eval_time = 0
            epoch_time = train_time + sample_time + eval_time
            total_time = gt.get_times().total

            # logger.record_tabular('Get Batch (s)', times_itrs['get_batch'][-1])
            # logger.record_tabular('Update Normalizer (s)', times_itrs['update_normalizer'][-1])
            # logger.record_tabular('Forward (s)', times_itrs['forward'][-1])
            # logger.record_tabular('Compute Losses (s)', times_itrs['compute_losses'][-1])
            # logger.record_tabular('QF1 Loop (s)', times_itrs['qf1_loop'][-1])
            # logger.record_tabular('QF2 Loop (s)', times_itrs['qf2_loop'][-1])
            # logger.record_tabular("Policy Forward (s)", times_itrs['policy_loss_forward'][-1])
            # logger.record_tabular('Policy Loop (s)', times_itrs['policy_loop'][-1])
            # logger.record_tabular('VF Loop (s)', times_itrs['vf_loop'][-1])

            [logger.record_tabular(key.title(), times_itrs[key][-1]) for key in times_itrs.keys()]

            logger.record_tabular('Train Time (s) ---', train_time)
            logger.record_tabular('(Previous) Eval Time (s) ---', eval_time)
            logger.record_tabular('Sample Time (s) ---', sample_time)
            logger.record_tabular('Epoch Time (s)', epoch_time)
            logger.record_tabular('Total Train Time (s)', total_time)
            logger.record_tabular("Epoch", epoch)

            table_keys = logger.get_table_key_set()
            if self._old_table_keys is not None and table_keys != self._old_table_keys:
                # assert table_keys == self._old_table_keys, (
                #     "Table keys cannot change from iteration to iteration."
                # )
                print("Table keys have changed. Rewriting header and filling with 0s")
                logger.update_header()
                raise NotImplementedError
            self._old_table_keys = table_keys

            logger.dump_tabular(with_prefix=False, with_timestamp=False)
        else:
            logger.log("Skipping eval for now.")

    def _can_evaluate(self):
        """
        One annoying thing about the logger table is that the keys at each
        iteration need to be the exact same. So unless you can compute
        everything, skip evaluation.

        A common example for why you might want to skip evaluation is that at
        the beginning of training, you may not have enough data for a
        validation and training set.
        """
        return (
            len(self._exploration_paths) > 0
            and not self.need_to_update_eval_statistics
        )

    def _can_train(self):
        return (
            self.replay_buffer.num_steps_can_sample() >=
            self.min_num_steps_before_training
        )

    def _get_action_and_info(self, observation):
        """
        Get an action to take in the environment.
        :param observation:
        :return:
        """
        self.exploration_policy.set_num_steps_total(self._n_env_steps_total)
        return self.exploration_policy.get_action(
            observation,
        )

    def _start_epoch(self, epoch):
        self._epoch_start_time = time.time()
        self._exploration_paths = []
        self._do_train_time = 0
        logger.push_prefix('Iteration #%d | ' % epoch)

    def _end_epoch(self, epoch):
        logger.log("Epoch Duration: {0}".format(
            time.time() - self._epoch_start_time
        ))
        logger.log("Started Training: {0}".format(self._can_train()))
        logger.pop_prefix()

        for post_epoch_func in self.post_epoch_funcs:
            post_epoch_func(self, epoch)

    def _start_new_rollout(self):
        self.exploration_policy.reset()
        return self.training_env.reset()

    def _handle_path(self, path):
        """
        Naive implementation: just loop through each transition.
        :param path:
        :return:
        """
        for (
            ob,
            action,
            reward,
            next_ob,
            terminal,
            agent_info,
            env_info
        ) in zip(
            path["observations"],
            path["actions"],
            path["rewards"],
            path["next_observations"],
            path["terminals"],
            path["agent_infos"],
            path["env_infos"],
        ):
            self._handle_step(
                ob,
                action,
                reward,
                next_ob,
                terminal,
                agent_info=agent_info,
                env_info=env_info,
            )
        self._handle_rollout_ending()

    def _handle_step(
            self,
            observation,
            action,
            reward,
            next_observation,
            terminal,
            agent_info,
            env_info,
            # full_observations
    ):
        """
        Implement anything that needs to happen after every step
        :return:
        """
        self._current_path_builder.add_all(
            observations=observation,
            actions=action,
            rewards=reward,
            next_observations=next_observation,
            terminals=terminal,
            agent_infos=agent_info,
            env_infos=env_info,
            # full_observations=full_observations,
        )
        self.replay_buffer.add_sample(
            observation=observation,
            action=action,
            reward=reward,
            terminal=terminal,
            next_observation=next_observation,
            agent_info=agent_info,
            env_info=env_info,
            # full_observations=full_observations,
        )

    def _handle_rollout_ending(self):
        """
        Implement anything that needs to happen after every rollout.
        """
        self.replay_buffer.terminate_episode()
        self._n_rollouts_total += 1

        if len(self._current_path_builder) > 0:
            path = self._current_path_builder.get_all_stacked()

            self._exploration_paths.append(path
            )
            self._current_path_builder = PathBuilder()

    def get_epoch_snapshot(self, epoch):
        data_to_save = dict(
            epoch=epoch,
            exploration_policy=self.exploration_policy,
            eval_policy=self.eval_policy,
        )
        if self.save_environment:
            data_to_save['env'] = self.training_env
        return data_to_save

    def get_extra_data_to_save(self, epoch):
        """
        Save things that shouldn't be saved every snapshot but rather
        overwritten every time.
        :param epoch:
        :return:
        """
        if self.render:
            self.training_env.render(close=True)
        data_to_save = dict(
            epoch=epoch,
        )
        if self.save_environment:
            data_to_save['env'] = self.training_env
        if self.save_replay_buffer:
            data_to_save['replay_buffer'] = self.replay_buffer
        if self.save_algorithm:
            data_to_save['algorithm'] = self
        return data_to_save

    @abc.abstractmethod
    def training_mode(self, mode):
        """
        Set training mode to `mode`.
        :param mode: If True, training will happen (e.g. set the dropout
        probabilities to not all ones).
        """
        pass

    def evaluate(self, epoch, eval_paths=None):
        statistics = OrderedDict()
        statistics.update(self.eval_statistics)

        logger.log("Collecting samples for evaluation")
        if eval_paths:
            test_paths = eval_paths
        else:
            test_paths = self.get_eval_paths()
        if hasattr(self.env.unwrapped, "num_blocks"):
            statistics.update(eval_util.get_generic_path_information(
                test_paths, stat_prefix="Test", num_blocks=self.env.unwrapped.num_blocks
            ))
            if len(self._exploration_paths) > 0:
                statistics.update(eval_util.get_generic_path_information(
                    self._exploration_paths, stat_prefix="Exploration"
                ))
        else:
            statistics.update(eval_util.get_generic_path_information(
                test_paths, stat_prefix="Test", num_blocks=None
            ))
            if len(self._exploration_paths) > 0:
                statistics.update(eval_util.get_generic_path_information(
                    self._exploration_paths, stat_prefix="Exploration", num_blocks=None
                ))
        if hasattr(self.env, "log_diagnostics"):
            self.env.log_diagnostics(test_paths, logger=logger)
        if hasattr(self.env, "get_diagnostics"):
            statistics.update(self.env.get_diagnostics(test_paths))

        average_returns = eval_util.get_average_returns(test_paths)
        statistics['AverageReturn'] = average_returns
        for key, value in statistics.items():
            logger.record_tabular(key, value)
        self.need_to_update_eval_statistics = True

    def get_eval_paths(self):
        return self.eval_sampler.obtain_samples()

    @abc.abstractmethod
    def _do_training(self):
        """
        Perform some update, e.g. perform one gradient step.
        :return:
        """
        pass


def set_to_train_mode(env):
    if hasattr(env, 'train'):
        env.train()


def set_to_eval_mode(env):
    if hasattr(env, 'eval'):
        env.eval()
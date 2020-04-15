# Author: bbrighttaer
# Project: GPMT
# Date: 4/8/2020
# Time: 8:02 PM
# File: reinforcement.py

from __future__ import absolute_import, division, print_function, unicode_literals

import argparse
import copy
import os
import random
import time
from datetime import datetime as dt

import numpy as np
import torch
import torch.nn as nn
from ptan.experience import ExperienceSourceFirstLast
from soek import Trainer, DataNode
from torch.utils.tensorboard import SummaryWriter
from tqdm import tqdm

from gpmt.data import GeneratorData
from gpmt.env import MoleculeEnv
from gpmt.model import Encoder, RewardNetRNN, StackRNN, StackedRNNDropout, StackedRNNLayerNorm, StackRNNLinear
from gpmt.reward import RewardFunction
from gpmt.rl import MolEnvProbabilityActionSelector, PolicyAgent, REINFORCE, GuidedRewardLearningIRL, \
    StateActionProbRegistry, Trajectory, EpisodeStep
from gpmt.utils import Flags, get_default_tokens, parse_optimizer, seq2tensor, init_hidden, init_cell, init_stack, \
    time_since

currentDT = dt.now()
date_label = currentDT.strftime("%Y_%m_%d__%H_%M_%S")

seeds = [1]

if torch.cuda.is_available():
    dvc_id = 0
    use_cuda = True
    device = 'cuda'
    torch.cuda.set_device(dvc_id)
else:
    device = 'cpu'
    use_cuda = None
    dvc_id = 0


def agent_net_hidden_states_func(num_layers, hidden_size, stack_depth, stack_width, unit_type, batch_size=1):
    return [get_initial_states(batch_size, hidden_size, 1, stack_depth, stack_width, unit_type) for _ in
            range(num_layers)]


def get_initial_states(batch_size, hidden_size, num_layers, stack_depth, stack_width, unit_type):
    hidden = init_hidden(num_layers=num_layers, batch_size=batch_size, hidden_size=hidden_size, num_dir=1,
                         dvc=f'{device}:{dvc_id}')
    if unit_type == 'lstm':
        cell = init_cell(num_layers=num_layers, batch_size=batch_size, hidden_size=hidden_size, num_dir=1,
                         dvc=f'{device}:{dvc_id}')
    else:
        cell = None
    stack = init_stack(batch_size, stack_width, stack_depth, dvc=f'{device}:{dvc_id}')
    return hidden, cell, stack


class IReLeaSE(Trainer):

    @staticmethod
    def initialize(hparams, gen_data, *args, **kwargs):
        # Embeddings provider
        encoder = Encoder(vocab_size=gen_data.n_characters, d_model=hparams['d_model'],
                          padding_idx=gen_data.char2idx[gen_data.pad_symbol],
                          dropout=hparams['dropout'], return_tuple=True)

        # Agent entities
        rnn_layers = []
        has_stack = True
        for i in range(1, hparams['agent_params']['num_layers'] + 1):
            rnn_layers.append(StackRNN(layer_index=i,
                                       input_size=hparams['d_model'],
                                       hidden_size=hparams['d_model'],
                                       has_stack=has_stack,
                                       unit_type=hparams['agent_params']['unit_type'],
                                       stack_width=hparams['agent_params']['stack_width'],
                                       stack_depth=hparams['agent_params']['stack_depth'],
                                       k_mask_func=encoder.k_padding_mask))
            rnn_layers.append(StackedRNNDropout(hparams['dropout']))
            rnn_layers.append(StackedRNNLayerNorm(hparams['d_model']))
        agent_net = nn.Sequential(encoder,
                                  *rnn_layers,
                                  StackRNNLinear(out_dim=gen_data.n_characters,
                                                 hidden_size=hparams['d_model'],
                                                 bidirectional=False,
                                                 bias=True))
        agent_net = agent_net.to(f'{device}:{dvc_id}')
        optimizer_agent_net = parse_optimizer(hparams['agent_params'], agent_net)
        selector = MolEnvProbabilityActionSelector(actions=gen_data.all_characters)
        probs_reg = StateActionProbRegistry()
        agent = PolicyAgent(model=agent_net,
                            action_selector=selector,
                            states_preprocessor=seq2tensor,
                            initial_state=lambda: agent_net_hidden_states_func(
                                num_layers=hparams['agent_params']['num_layers'],
                                hidden_size=hparams['d_model'],
                                stack_depth=hparams['agent_params']['stack_depth'],
                                stack_width=hparams['agent_params']['stack_width'],
                                unit_type=hparams['agent_params']['unit_type']),
                            apply_softmax=True,
                            probs_registry=probs_reg,
                            device=f'{device}:{dvc_id}')
        drl_alg = REINFORCE(model=agent_net, optimizer=optimizer_agent_net,
                            initial_states_func=lambda bz: agent_net_hidden_states_func(
                                num_layers=hparams['agent_params']['num_layers'],
                                hidden_size=hparams['d_model'],
                                stack_depth=hparams['agent_params']['stack_depth'],
                                stack_width=hparams['agent_params']['stack_width'],
                                unit_type=hparams['agent_params']['unit_type'], batch_size=bz))

        # Reward function entities
        reward_net = nn.Sequential(encoder,
                                   RewardNetRNN(input_size=hparams['d_model'],
                                                hidden_size=hparams['d_model'],
                                                num_layers=hparams['reward_params']['num_layers'],
                                                bidirectional=True,
                                                dropout=hparams['dropout'],
                                                unit_type=hparams['reward_params']['unit_type']))
        reward_net = reward_net.to(f'{device}:{dvc_id}')
        reward_function = RewardFunction(reward_net, mc_policy=agent, actions=gen_data.all_characters,
                                         device=f'{device}:{dvc_id}',
                                         mc_max_sims=hparams['monte_carlo_N'],
                                         expert_func=None)
        optimizer_reward_net = parse_optimizer(hparams['reward_params'], reward_net)
        gen_data.set_batch_size(hparams['reward_params']['batch_size'])
        irl_alg = GuidedRewardLearningIRL(reward_net, optimizer_reward_net, gen_data,
                                          k=hparams['reward_params']['irl_alg_num_iter'])

        init_args = {'agent': agent,
                     'probs_reg': probs_reg,
                     'drl_alg': drl_alg,
                     'irl_alg': irl_alg,
                     'reward_func': reward_function,
                     'gamma': hparams['gamma'],
                     'episodes_to_train': hparams['episodes_to_train']}
        return init_args

    @staticmethod
    def data_provider(k, flags):
        tokens = get_default_tokens()
        gen_data = GeneratorData(training_data_path=flags.demo_file,
                                 delimiter='\t',
                                 cols_to_read=[0],
                                 keep_header=True,
                                 pad_symbol=' ',
                                 max_len=120,
                                 tokens=tokens,
                                 use_cuda=use_cuda)
        return {"train": gen_data, "val": gen_data, "test": gen_data}

    @staticmethod
    def evaluate(*args, **kwargs):
        super().evaluate(*args, **kwargs)

    @staticmethod
    def train(init_args, agent_net_path=None, agent_net_name=None, seed=0, n_episodes=5000, sim_data_node=None,
              tb_writer=None, is_hsearch=False):
        agent = init_args['agent']
        probs_reg = init_args['probs_reg']
        drl_algorithm = init_args['drl_alg']
        irl_algorithm = init_args['irl_alg']
        reward_func = init_args['reward_func']
        gamma = init_args['gamma']
        episodes_to_train = init_args['episodes_to_train']
        score_threshold = 0.
        best_model_wts = None
        best_score = None

        # load pretrained model
        if agent_net_path and agent_net_name:
            agent.model.load_state_dict(IReLeaSE.load_model(agent_net_path, agent_net_name))

        tb_writer = None  # tb_writer()
        start = time.time()

        # Create environment
        env = MoleculeEnv(actions=get_default_tokens(), reward_func=reward_func, seed=seed)

        # Leverage PTAN mechanisms
        exp_source = ExperienceSourceFirstLast(env, agent, gamma, steps_count=1, steps_delta=1)

        # Begin simulation and training
        total_rewards, batch_states, batch_actions, batch_qvals, cur_rewards = [], [], [], [], []
        trajectories = []
        done_episodes = 0
        batch_episodes = 0
        step_idx = 0
        traj_prob = 1.
        for step_idx, exp in tqdm(enumerate(exp_source)):
            batch_states.append(exp.state)
            batch_actions.append(exp.action)
            cur_rewards.append(exp.reward)
            traj_prob *= probs_reg.get(list(exp.state), exp.action)

            # Once the current episode is done calculate the Q-values of all state-action pairs in the episode
            if exp.last_state is None:
                batch_qvals.extend(calc_Qvals(cur_rewards, gamma))
                trajectories.append(Trajectory(terminal_state=EpisodeStep(exp.state, exp.action, batch_qvals[-1]),
                                               traj_prob=traj_prob))
                traj_prob = 1.
                cur_rewards.clear()
                probs_reg.clear()
                batch_episodes += 1

            new_rewards = exp_source.pop_total_rewards()
            if new_rewards:
                done_episodes += 1
                reward = new_rewards[0]
                total_rewards.append(reward)
                mean_rewards = float(np.mean(total_rewards[-100:]))
                print(f'Time = {time_since(start)}, step = {step_idx}, reward = {reward:6.2f}, '
                      f'mean_100 = {mean_rewards:6.2f}, episodes = {done_episodes}')
                if mean_rewards >= score_threshold:
                    best_model_wts = [copy.deepcopy(agent.model.state_dict()),
                                      copy.deepcopy(reward_func.model.state_dict())]
                    best_score = mean_rewards
                    score_threshold = best_score

            if batch_episodes < episodes_to_train:
                continue

            # Train models
            print('Fitting models...')
            irl_loss = irl_algorithm.fit(trajectories)
            rl_loss = drl_algorithm.fit(states=batch_states, actions=batch_actions, qvals=batch_qvals)
            print(f'IRL loss = {irl_loss}, RL loss = {rl_loss}')

            if batch_episodes == n_episodes:
                print('Training completed!')
                break

            # Reset
            batch_episodes = 0
            batch_states.clear()
            batch_actions.clear()
            batch_qvals.clear()
            trajectories.clear()

        return {'model': [agent.model.load_state_dict(best_model_wts[0]),
                          reward_func.model.load_state_dict(best_model_wts[1])],
                'score': best_score,
                'epoch': step_idx}

    @staticmethod
    def evaluate_model(*args, **kwargs):
        super().evaluate_model(*args, **kwargs)

    @staticmethod
    def save_model(model, path, name):
        os.makedirs(path, exist_ok=True)
        file = os.path.join(path, name + ".mod")
        torch.save(model.state_dict(), file)

    @staticmethod
    def load_model(path, name):
        return torch.load(os.path.join(path, name), map_location=torch.device(f'{device}:{dvc_id}'))


def calc_Qvals(rewards, gamma):
    qval = []
    sum_r = 0.
    for r in reversed(rewards):
        sum_r *= gamma
        sum_r += r
        qval.append(sum_r)
    return list(reversed(qval))


def main(flags):
    sim_label = 'DeNovo-IReLeaSE'
    sim_data = DataNode(label=sim_label)
    nodes_list = []
    sim_data.data = nodes_list

    # For searching over multiple seeds
    hparam_search = None

    for seed in seeds:
        summary_writer_creator = lambda: SummaryWriter(log_dir="tb_gpmt"
                                                               "/{}_{}_{}/".format(sim_label, seed, dt.now().strftime(
            "%Y_%m_%d__%H_%M_%S")))

        # for data collection of this round of simulation.
        data_node = DataNode(label="seed_%d" % seed)
        nodes_list.append(data_node)

        random.seed(seed)
        np.random.seed(seed)
        torch.manual_seed(seed)
        torch.cuda.manual_seed_all(seed)

        print('-----------------------------------------------------------------')
        print(f'{sim_label}\tDemonstrations file: {flags.demo_file}')
        print('-----------------------------------------------------------------')

        irelease = IReLeaSE()
        k = 1
        if flags.hparam_search:
            pass
        else:
            hyper_params = default_hparams(flags)
            init_args = irelease.initialize(hyper_params, irelease.data_provider(k, flags)['train'])
            results = irelease.train(init_args, flags.model_dir, flags.pretrained_model, seed, sim_data_node=data_node,
                                     tb_writer=summary_writer_creator, )

    # save simulation data resource tree to file.
    sim_data.to_json(path="./analysis/")


def default_hparams(args):
    return {'d_model': 64,
            'dropout': 0.0,
            'monte_carlo_N': 50,
            'gamma': 0.97,
            'episodes_to_train': 10,
            'reward_params': {'num_layers': 1,
                              'unit_type': 'gru',
                              'batch_size': 64,
                              'irl_alg_num_iter': 10,
                              'optimizer': 'adam',
                              'optimizer__global__weight_decay': 0.00005,
                              'optimizer__global__lr': 0.001,
                              },
            'agent_params': {'unit_type': 'gru',
                             'num_layers': 1,
                             'stack_width': 64,
                             'stack_depth': 20,
                             'optimizer': 'adadelta',
                             'optimizer__global__weight_decay': 0.00005,
                             'optimizer__global__lr': 0.001,
                             }}


def get_hparam_config(args):
    pass


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='IRL for Structural Evolution of Small Molecules')
    parser.add_argument('-d', '--demo', dest='demo_file', type=str,
                        help='File containing SMILES strings which are demonstrations of the required objective')
    parser.add_argument('--model_dir',
                        type=str,
                        default=None,
                        help='Directory containing models')
    parser.add_argument('--pretrained_model',
                        type=str,
                        default=None,
                        help='The name of the pretrained model')
    parser.add_argument("--hparam_search", action="store_true",
                        help="If true, hyperparameter searching would be performed.")
    parser.add_argument("--hparam_search_alg",
                        type=str,
                        default="bayopt_search",
                        help="Hyperparameter search algorithm to use. One of [bayopt_search, random_search]")

    args = parser.parse_args()
    flags = Flags()
    args_dict = args.__dict__
    for arg in args_dict:
        setattr(flags, arg, args_dict[arg])
    main(flags)

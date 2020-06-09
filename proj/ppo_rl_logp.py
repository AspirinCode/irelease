# Author: bbrighttaer
# Project: GPMT
# Date: 4/8/2020
# Time: 8:02 PM
# File: ppo_rl_logp.py

from __future__ import absolute_import, division, print_function, unicode_literals

import argparse
import contextlib
import copy
import os
import random
import time
from datetime import datetime as dt

import numpy as np
import torch
import torch.nn as nn
from ptan.common.utils import TBMeanTracker
from ptan.experience import ExperienceSourceFirstLast
from soek import Trainer, DataNode, ConstantParam, DictParam, LogRealParam, CategoricalParam, DiscreteParam, RealParam, \
    BayesianOptSearch, RandomSearch
from soek.bopt import GPMinArgs
from torch.utils.tensorboard import SummaryWriter
from tqdm import tqdm

from irelease.data import GeneratorData
from irelease.env import MoleculeEnv
from irelease.model import Encoder, StackRNN, StackRNNLinear, \
    CriticRNN, RewardNetRNN, StackedRNNDropout, StackedRNNLayerNorm
from irelease.predictor import RNNPredictor, get_logp_reward
from irelease.reward import RewardFunction
from irelease.rl import MolEnvProbabilityActionSelector, PolicyAgent, GuidedRewardLearningIRL, \
    StateActionProbRegistry, Trajectory, EpisodeStep, PPO
from irelease.utils import Flags, get_default_tokens, parse_optimizer, seq2tensor, init_hidden, init_cell, init_stack, \
    time_since, generate_smiles, ExpAverage, DummyException
from mol_metrics import verify_sequence, get_mol_metrics

currentDT = dt.now()
date_label = currentDT.strftime("%Y_%m_%d__%H_%M_%S")

seeds = [1]

if torch.cuda.is_available():
    dvc_id = 0
    use_cuda = True
    device = f'cuda:{dvc_id}'
    torch.cuda.set_device(dvc_id)
else:
    device = 'cpu'
    use_cuda = None


def agent_net_hidden_states_func(batch_size, num_layers, hidden_size, stack_depth, stack_width, unit_type):
    return [get_initial_states(batch_size, hidden_size, 1, stack_depth, stack_width, unit_type) for _ in
            range(num_layers)]


def get_initial_states(batch_size, hidden_size, num_layers, stack_depth, stack_width, unit_type):
    hidden = init_hidden(num_layers=num_layers, batch_size=batch_size, hidden_size=hidden_size, num_dir=1,
                         dvc=device)
    if unit_type == 'lstm':
        cell = init_cell(num_layers=num_layers, batch_size=batch_size, hidden_size=hidden_size, num_dir=1,
                         dvc=device)
    else:
        cell = None
    stack = init_stack(batch_size, stack_width, stack_depth, dvc=device)
    return hidden, cell, stack


class IReLeaSE(Trainer):

    @staticmethod
    def initialize(hparams, demo_data_gen, unbiased_data_gen, prior_data_gen, *args, **kwargs):
        # Embeddings provider
        encoder = Encoder(vocab_size=demo_data_gen.n_characters, d_model=hparams['d_model'],
                          padding_idx=demo_data_gen.char2idx[demo_data_gen.pad_symbol],
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
            if hparams['agent_params']['num_layers'] > 1:
                rnn_layers.append(StackedRNNDropout(hparams['dropout']))
                rnn_layers.append(StackedRNNLayerNorm(hparams['d_model']))
        agent_net = nn.Sequential(encoder,
                                  *rnn_layers,
                                  StackRNNLinear(out_dim=demo_data_gen.n_characters,
                                                 hidden_size=hparams['d_model'],
                                                 bidirectional=False,
                                                 bias=True))
        with contextlib.suppress(Exception):
            agent_net = agent_net.to(device)
        optimizer_agent_net = parse_optimizer(hparams['agent_params'], agent_net)
        selector = MolEnvProbabilityActionSelector(actions=demo_data_gen.all_characters)
        probs_reg = StateActionProbRegistry()
        init_state_args = {'num_layers': hparams['agent_params']['num_layers'],
                           'hidden_size': hparams['d_model'],
                           'stack_depth': hparams['agent_params']['stack_depth'],
                           'stack_width': hparams['agent_params']['stack_width'],
                           'unit_type': hparams['agent_params']['unit_type']}
        agent = PolicyAgent(model=agent_net,
                            action_selector=selector,
                            states_preprocessor=seq2tensor,
                            initial_state=agent_net_hidden_states_func,
                            initial_state_args=init_state_args,
                            apply_softmax=True,
                            probs_registry=probs_reg,
                            device=device)
        critic = nn.Sequential(encoder,
                               CriticRNN(hparams['d_model'], hparams['critic_params']['d_model'],
                                         unit_type=hparams['critic_params']['unit_type'],
                                         dropout=hparams['critic_params']['dropout'],
                                         num_layers=hparams['critic_params']['num_layers']))
        with contextlib.suppress(Exception):
            critic = critic.to(device)
        optimizer_critic_net = parse_optimizer(hparams['critic_params'], critic)
        drl_alg = PPO(actor=agent_net, actor_opt=optimizer_agent_net,
                      critic=critic, critic_opt=optimizer_critic_net,
                      initial_states_func=agent_net_hidden_states_func,
                      initial_states_args=init_state_args,
                      device=device,
                      gamma=hparams['gamma'],
                      gae_lambda=hparams['gae_lambda'],
                      ppo_eps=hparams['ppo_eps'],
                      ppo_epochs=hparams['ppo_epochs'],
                      ppo_batch=hparams['ppo_batch'])

        # Reward function entities
        reward_net = nn.Sequential(encoder,
                                   RewardNetRNN(input_size=hparams['d_model'],
                                                hidden_size=hparams['reward_params']['d_model'],
                                                num_layers=hparams['reward_params']['num_layers'],
                                                bidirectional=hparams['reward_params']['bidirectional'],
                                                use_attention=hparams['reward_params']['use_attention'],
                                                dropout=hparams['reward_params']['dropout'],
                                                unit_type=hparams['reward_params']['unit_type']))
        with contextlib.suppress(Exception):
            reward_net = reward_net.to(device)
        expert_model = RNNPredictor(hparams['expert_model_params'], device)
        reward_function = RewardFunction(reward_net, mc_policy=agent, actions=demo_data_gen.all_characters,
                                         device=device, use_mc=hparams['use_monte_carlo_sim'],
                                         mc_max_sims=hparams['monte_carlo_N'],
                                         expert_func=expert_model,
                                         use_true_reward=hparams['use_true_reward'],
                                         true_reward_func=get_logp_reward,
                                         no_mc_fill_val=hparams['no_mc_fill_val'])
        optimizer_reward_net = parse_optimizer(hparams['reward_params'], reward_net)
        demo_data_gen.set_batch_size(hparams['reward_params']['demo_batch_size'])
        irl_alg = GuidedRewardLearningIRL(reward_net, optimizer_reward_net, demo_data_gen,
                                          k=hparams['reward_params']['irl_alg_num_iter'],
                                          agent_net=agent_net,
                                          agent_net_init_func=agent_net_hidden_states_func,
                                          agent_net_init_func_args=init_state_args,
                                          device=device)

        init_args = {'agent': agent,
                     'probs_reg': probs_reg,
                     'drl_alg': drl_alg,
                     'irl_alg': irl_alg,
                     'reward_func': reward_function,
                     'gamma': hparams['gamma'],
                     'episodes_to_train': hparams['episodes_to_train'],
                     'expert_model': expert_model,
                     'demo_data_gen': demo_data_gen,
                     'unbiased_data_gen': unbiased_data_gen,
                     'gen_args': {'num_layers': hparams['agent_params']['num_layers'],
                                  'hidden_size': hparams['d_model'],
                                  'num_dir': 1,
                                  'stack_depth': hparams['agent_params']['stack_depth'],
                                  'stack_width': hparams['agent_params']['stack_width'],
                                  'has_stack': has_stack,
                                  'has_cell': hparams['agent_params']['unit_type'] == 'lstm',
                                  'device': device}}
        return init_args

    @staticmethod
    def data_provider(k, flags):
        tokens = get_default_tokens()
        demo_data = GeneratorData(training_data_path=flags.demo_file,
                                  delimiter='\t',
                                  cols_to_read=[0],
                                  keep_header=True,
                                  pad_symbol=' ',
                                  max_len=120,
                                  tokens=tokens,
                                  use_cuda=use_cuda)
        unbiased_data = GeneratorData(training_data_path=flags.unbiased_file,
                                      delimiter='\t',
                                      cols_to_read=[0],
                                      keep_header=True,
                                      pad_symbol=' ',
                                      max_len=120,
                                      tokens=tokens,
                                      use_cuda=use_cuda)
        prior_data = GeneratorData(training_data_path=flags.prior_data,
                                   delimiter='\t',
                                   cols_to_read=[0],
                                   keep_header=True,
                                   pad_symbol=' ',
                                   max_len=120,
                                   tokens=tokens,
                                   use_cuda=use_cuda)
        return {'demo_data': demo_data, 'unbiased_data': unbiased_data, 'prior_data': prior_data}

    @staticmethod
    def evaluate(res_dict, generated_smiles, ref_smiles):
        smiles = []
        for s in generated_smiles:
            if verify_sequence(s):
                smiles.append(s)
        mol_metrics = get_mol_metrics()
        for metric in mol_metrics:
            res_dict[metric] = mol_metrics[metric](smiles, ref_smiles)
        score = res_dict['internal_diversity']
        return score

    @staticmethod
    def train(init_args, agent_net_path=None, agent_net_name=None, seed=0, n_episodes=500, sim_data_node=None,
              tb_writer=None, is_hsearch=False, n_to_generate=200, learn_irl=True):
        tb_writer = tb_writer()
        agent = init_args['agent']
        probs_reg = init_args['probs_reg']
        drl_algorithm = init_args['drl_alg']
        irl_algorithm = init_args['irl_alg']
        reward_func = init_args['reward_func']
        gamma = init_args['gamma']
        episodes_to_train = init_args['episodes_to_train']
        expert_model = init_args['expert_model']
        demo_data_gen = init_args['demo_data_gen']
        unbiased_data_gen = init_args['unbiased_data_gen']
        best_model_wts = None
        best_score = 0.
        exp_avg = ExpAverage(beta=0.6)
        score_threshold = 3.6

        # load pretrained model
        if agent_net_path and agent_net_name:
            print('Loading pretrained model...')
            weights = IReLeaSE.load_model(agent_net_path, agent_net_name)
            agent.model.load_state_dict(weights)
            print('Pretrained model loaded successfully!')

        start = time.time()

        # Begin simulation and training
        total_rewards = []
        trajectories = []
        done_episodes = 0
        batch_episodes = 0
        exp_trajectories = []
        step_idx = 0

        env = MoleculeEnv(actions=get_default_tokens(), reward_func=reward_func)
        exp_source = ExperienceSourceFirstLast(env, agent, gamma, steps_count=1, steps_delta=1)
        traj_prob = 1.
        exp_traj = []

        reference_smiles = demo_data_gen.random_training_set_smiles(1000)
        demo_score = np.mean(expert_model(reference_smiles)[1])
        baseline_score = np.mean(expert_model(unbiased_data_gen.random_training_set_smiles(1000))[1])
        with contextlib.suppress(Exception if is_hsearch else DummyException):
            with TBMeanTracker(tb_writer, 1) as tracker:
                for step_idx, exp in tqdm(enumerate(exp_source)):
                    exp_traj.append(exp)
                    traj_prob *= probs_reg.get(list(exp.state), exp.action)

                    if exp.last_state is None:
                        trajectories.append(Trajectory(terminal_state=EpisodeStep(exp.state, exp.action),
                                                       traj_prob=traj_prob))
                        exp_trajectories.append(exp_traj)  # for ExperienceFirstLast objects
                        exp_traj = []
                        traj_prob = 1.
                        probs_reg.clear()
                        batch_episodes += 1

                    new_rewards = exp_source.pop_total_rewards()
                    if new_rewards:
                        reward = new_rewards[0]
                        done_episodes += 1
                        total_rewards.append(reward)
                        mean_rewards = float(np.mean(total_rewards[-100:]))
                        tracker.track('mean_total_reward', mean_rewards, step_idx)
                        tracker.track('total_reward', reward, step_idx)
                        print(f'Time = {time_since(start)}, step = {step_idx}, reward = {reward:6.2f}, '
                              f'mean_100 = {mean_rewards:6.2f}, episodes = {done_episodes}')
                        with torch.set_grad_enabled(False):
                            samples = generate_smiles(drl_algorithm.model, demo_data_gen, init_args['gen_args'],
                                                      num_samples=n_to_generate)
                        predictions = expert_model(samples)[1]
                        mean_preds = np.mean(predictions)
                        try:
                            percentage_in_threshold = np.sum((predictions >= 0.0) &
                                                             (predictions <= 5.0)) / len(predictions)
                        except:
                            percentage_in_threshold = 0.
                        per_valid = len(predictions) / n_to_generate
                        print(f'Mean value of predictions = {mean_preds}, '
                              f'% of valid SMILES = {per_valid}, '
                              f'% in drug-like region={percentage_in_threshold}')
                        tb_writer.add_scalars('qsar_score', {'sampled': mean_preds,
                                                             'baseline': baseline_score,
                                                             'demo_data': demo_score}, step_idx)
                        tb_writer.add_scalars('SMILES stats', {'per. of valid': per_valid,
                                                               'per. in drug-like region': percentage_in_threshold},
                                              step_idx)
                        eval_dict = {}
                        eval_score = IReLeaSE.evaluate(eval_dict, samples, reference_smiles)
                        for k in eval_dict:
                            tracker.track(k, eval_dict[k], step_idx)
                        avg_len = np.nanmean([len(s) for s in samples])
                        tracker.track('Average SMILES length', avg_len, step_idx)
                        diversity = 0 if eval_score >= 0.2 else np.log(eval_score)
                        smile_length = 0 if avg_len >= 20 else -np.exp(mean_preds)
                        score = 2 * np.exp(mean_preds) + max(-np.exp(mean_preds), np.log(per_valid)) + max(
                            -np.exp(mean_preds), diversity) + smile_length
                        tracker.track('score', score, step_idx)
                        exp_avg.update(mean_preds)
                        if exp_avg.value > best_score:
                            best_model_wts = [copy.deepcopy(drl_algorithm.actor.state_dict()),
                                              copy.deepcopy(drl_algorithm.critic.state_dict()),
                                              copy.deepcopy(irl_algorithm.model.state_dict())]
                            best_score = exp_avg.value
                            # break

                        if done_episodes == n_episodes:
                            print('Training completed!')
                            break

                    if batch_episodes < episodes_to_train:
                        continue

                    # Train models
                    print('Fitting models...')
                    irl_loss = irl_algorithm.fit(trajectories)
                    rl_loss = drl_algorithm.fit(exp_trajectories)
                    samples = generate_smiles(drl_algorithm.model, demo_data_gen, init_args['gen_args'],
                                              num_samples=3)
                    print(f'IRL loss = {irl_loss}, RL loss = {rl_loss}, samples = {samples}')
                    tracker.track('irl_loss', irl_loss, step_idx)
                    tracker.track('critic_loss', rl_loss[0], step_idx)
                    tracker.track('agent_loss', rl_loss[1], step_idx)

                    # Reset
                    batch_episodes = 0
                    trajectories.clear()
                    exp_trajectories.clear()

        if best_model_wts:
            drl_algorithm.actor.load_state_dict(best_model_wts[0])
            drl_algorithm.critic.load_state_dict(best_model_wts[1])
            irl_algorithm.model.load_state_dict(best_model_wts[2])
        duration = time.time() - start
        print('\nTraining duration: {:.0f}m {:.0f}s'.format(duration // 60, duration % 60))
        return {'model': [drl_algorithm.actor,
                          drl_algorithm.critic,
                          irl_algorithm.model],
                'score': round(best_score, 3),
                'epoch': done_episodes}

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
        return torch.load(os.path.join(path, name), map_location=torch.device(device))


def main(flags):
    sim_label = flags.exp_name + '_IReLeaSE-ppo_with_irl_' + ('attn' if flags.use_attention else 'no_attn')
    sim_data = DataNode(label=sim_label)
    nodes_list = []
    sim_data.data = nodes_list

    for seed in seeds:
        summary_writer_creator = lambda: SummaryWriter(log_dir="irelease"
                                                               "/{}_{}_{}/".format(sim_label, seed, dt.now().strftime(
            "%Y_%m_%d__%H_%M_%S")))

        # for data collection of this round of simulation.
        data_node = DataNode(label="seed_%d" % seed)
        nodes_list.append(data_node)

        random.seed(seed)
        np.random.seed(seed)
        torch.manual_seed(seed)
        torch.cuda.manual_seed_all(seed)

        print('--------------------------------------------------------------------------------')
        print(f'{device}\n{sim_label}\tDemonstrations file: {flags.demo_file}')
        print('--------------------------------------------------------------------------------')

        irelease = IReLeaSE()
        k = 1
        if flags.hparam_search:
            print(f'Hyperparameter search enabled: {flags.hparam_search_alg}')
            # arguments to callables
            extra_init_args = {}
            extra_data_args = {'flags': flags}
            extra_train_args = {'agent_net_path': flags.model_dir,
                                'agent_net_name': flags.pretrained_model,
                                'seed': seed,
                                'n_episodes': 300,
                                'is_hsearch': True,
                                'tb_writer': summary_writer_creator}
            hparams_conf = get_hparam_config(flags)
            search_alg = {'random_search': RandomSearch,
                          'bayopt_search': BayesianOptSearch}.get(flags.hparam_search_alg,
                                                                  BayesianOptSearch)
            search_args = GPMinArgs(n_calls=20, random_state=seed)
            hparam_search = search_alg(hparam_config=hparams_conf,
                                       num_folds=1,
                                       initializer=irelease.initialize,
                                       data_provider=irelease.data_provider,
                                       train_fn=irelease.train,
                                       save_model_fn=irelease.save_model,
                                       alg_args=search_args,
                                       init_args=extra_init_args,
                                       data_args=extra_data_args,
                                       train_args=extra_train_args,
                                       data_node=data_node,
                                       split_label='ppo-rl',
                                       sim_label=sim_label,
                                       dataset_label=None,
                                       results_file=f'{flags.hparam_search_alg}_{sim_label}'
                                                    f'_{date_label}_seed_{seed}')
            start = time.time()
            stats = hparam_search.fit()
            print(f'Duration = {time_since(start)}')
            print(stats)
            print("\nBest params = {}, duration={}".format(stats.best(), time_since(start)))
        else:
            hyper_params = default_hparams(flags)
            data_gens = irelease.data_provider(k, flags)
            init_args = irelease.initialize(hyper_params, data_gens['demo_data'], data_gens['unbiased_data'],
                                            data_gens['prior_data'])
            results = irelease.train(init_args, flags.model_dir, flags.pretrained_model, seed,
                                     sim_data_node=data_node,
                                     n_episodes=500,
                                     learn_irl=not flags.use_true_reward,
                                     tb_writer=summary_writer_creator)
            irelease.save_model(results['model'][0],
                                path=flags.model_dir,
                                name=f'{flags.exp_name}_irelease_stack-rnn_{hyper_params["agent_params"]["unit_type"]}'
                                     f'_ppo_agent_{date_label}_{results["score"]}_{results["epoch"]}')
            irelease.save_model(results['model'][1],
                                path=flags.model_dir,
                                name=f'{flags.exp_name}_irelease_stack-rnn_{hyper_params["agent_params"]["unit_type"]}'
                                     f'_ppo_critic_{date_label}_{results["score"]}_{results["epoch"]}')
            irelease.save_model(results['model'][2],
                                path=flags.model_dir,
                                name=f'{flags.exp_name}_irelease_stack-rnn_{hyper_params["agent_params"]["unit_type"]}'
                                     f'_reward_net_{date_label}_{results["score"]}_{results["epoch"]}')

    # save simulation data resource tree to file.
    sim_data.to_json(path="./analysis/")


def default_hparams(args):
    return {'d_model': 1500,
            'dropout': 0.0,
            'monte_carlo_N': 5,
            'use_monte_carlo_sim': True,
            'no_mc_fill_val': 0.0,
            'gamma': 0.97,
            'episodes_to_train': 10,
            'gae_lambda': 0.95,
            'ppo_eps': 0.2,
            'ppo_batch': 1,
            'ppo_epochs': 5,
            'use_true_reward': args.use_true_reward,
            'reward_params': {'num_layers': 2,
                              'd_model': 512,
                              'unit_type': 'gru',
                              'demo_batch_size': 32,
                              'irl_alg_num_iter': 5,
                              'dropout': 0.2,
                              'use_attention': args.use_attention,
                              'bidirectional': True,
                              'optimizer': 'adadelta',
                              'optimizer__global__weight_decay': 0.0005,
                              'optimizer__global__lr': 0.001, },
            'agent_params': {'unit_type': 'gru',
                             'num_layers': 2,
                             'stack_width': 1500,
                             'stack_depth': 200,
                             'optimizer': 'adadelta',
                             'optimizer__global__weight_decay': 0.0000,
                             'optimizer__global__lr': 0.001},
            'critic_params': {'num_layers': 2,
                              'd_model': 256,
                              'dropout': 0.2,
                              'unit_type': 'lstm',
                              'optimizer': 'adadelta',
                              'optimizer__global__weight_decay': 0.00005,
                              'optimizer__global__lr': 0.001},
            'expert_model_params': {'model_dir': './model_dir/expert_rnn_reg',
                                    'd_model': 128,
                                    'rnn_num_layers': 2,
                                    'dropout': 0.8,
                                    'is_bidirectional': False,
                                    'unit_type': 'lstm'}
            }


def get_hparam_config(args):
    return {'d_model': ConstantParam(1500),
            'dropout': RealParam(min=0.),
            'monte_carlo_N': ConstantParam(5),
            'use_monte_carlo_sim': ConstantParam(True),
            'no_mc_fill_val': ConstantParam(0.0),
            'gamma': ConstantParam(0.97),
            'episodes_to_train': DiscreteParam(min=5, max=20),
            'gae_lambda': RealParam(0.9, max=0.999),
            'ppo_eps': ConstantParam(0.2),
            'ppo_batch': ConstantParam(1),
            'ppo_epochs': DiscreteParam(2, max=10),
            'use_true_reward': ConstantParam(args.use_true_reward),
            'reward_params': DictParam({'num_layers': DiscreteParam(min=1, max=4),
                                        'd_model': DiscreteParam(min=128, max=1024),
                                        'unit_type': ConstantParam('lstm'),
                                        'demo_batch_size': CategoricalParam([64, 128, 256]),
                                        'irl_alg_num_iter': DiscreteParam(2, max=10),
                                        'use_attention': ConstantParam(False),
                                        'bidirectional': ConstantParam(True),
                                        'dropout': RealParam(),
                                        'optimizer': CategoricalParam(
                                            choices=['sgd', 'adam', 'adadelta', 'adagrad', 'adamax', 'rmsprop']),
                                        'optimizer__global__weight_decay': LogRealParam(),
                                        'optimizer__global__lr': LogRealParam()}),
            'agent_params': DictParam({'unit_type': ConstantParam('gru'),
                                       'num_layers': ConstantParam(2),
                                       'stack_width': ConstantParam(1500),
                                       'stack_depth': ConstantParam(200),
                                       'optimizer': ConstantParam('adadelta'),
                                       'optimizer__global__weight_decay': LogRealParam(),
                                       'optimizer__global__lr': LogRealParam()}),
            'critic_params': DictParam({'num_layers': ConstantParam(2),
                                        'd_model': ConstantParam(256),
                                        'dropout': RealParam(),
                                        'unit_type': ConstantParam('lstm'),
                                        'optimizer': ConstantParam('adadelta'),
                                        'optimizer__global__weight_decay': LogRealParam(),
                                        'optimizer__global__lr': LogRealParam()}),
            'expert_model_params': DictParam({'model_dir': ConstantParam('./model_dir/expert_rnn_reg'),
                                              'd_model': ConstantParam(128),
                                              'rnn_num_layers': ConstantParam(2),
                                              'dropout': ConstantParam(0.8),
                                              'is_bidirectional': ConstantParam(False),
                                              'unit_type': ConstantParam('lstm')})
            }


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='IRL for Structural Evolution of Small Molecules')
    parser.add_argument('--exp_name', type=str,
                        help='Name for the experiment. This would be added to saved model names')
    parser.add_argument('--demo', dest='demo_file', type=str,
                        help='File containing SMILES strings which are demonstrations of the required objective')
    parser.add_argument('--unbiased', dest='unbiased_file', type=str,
                        help='File containing SMILES generated with the pretrained (prior) model.')
    parser.add_argument('--prior_data', dest='prior_data', type=str,
                        help='File containing SMILES used to train the prior model.')
    parser.add_argument('--model_dir',
                        type=str,
                        default='./model_dir',
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
    parser.add_argument('--use_attention',
                        action='store_true',
                        help='Whether to use additive attention')
    parser.add_argument('--use_true_reward',
                        action='store_true',
                        help='If true then no reward function would be learned but the true reward would be used.'
                             'This requires that the explicit reward function is given.')

    args = parser.parse_args()
    flags = Flags()
    args_dict = args.__dict__
    for arg in args_dict:
        setattr(flags, arg, args_dict[arg])
    main(flags)

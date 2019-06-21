import torch
import torch.nn as nn
import torch.nn.functional as F 
import torch.autograd
from torch.autograd import Variable

from torch.optim import Adam

from ddpg.replay_buffer import DDPGBuffer, Transition
from ddpg.model import Actor, Critic

import numpy as np

import os
import time as time

"""
DDPG Utils
"""

def soft_update(target, source, tau):
    for target_param, param in zip(target.parameters(), source.parameters()):
        target_param.data.copy_(target_param.data * (1.0 - tau) + param.data * tau)

def hard_update(target, source):
    for target_param, param in zip(target.parameters(), source.parameters()):
        target_param.data.copy_(param.data)

def ddpg_distance_metric(actions1, actions2):
    """
    Compute "distance" between actions taken by two policies at the same states
    Expects numpy arrays
    """
    diff = actions1-actions2
    mean_diff = np.mean(np.square(diff), axis=0)
    dist = sqrt(np.mean(mean_diff))
    return dist


class DDPG(object):
    def __init__(self, gamma, tau, hidden_size, num_inputs, action_space):

        self.num_inputs = num_inputs
        self.action_space = action_space

        """
        Initialize actor and critic networks. Also initialize target networks
        """
        self.actor = Actor(hidden_size, self.num_inputs, self.action_space)
        self.actor_target = Actor(hidden_size, self.num_inputs, self.action_space)
        self.actor_perturbed = Actor(hidden_size, self.num_inputs, self.action_space)
        self.actor_optim = Adam(self.actor.parameters(), lr=1e-4)

        self.critic = Critic(hidden_size, self.num_inputs, self.action_space)
        self.critic_target = Critic(hidden_size, self.num_inputs, self.action_space)
        self.critic_optim = Adam(self.critic.parameters(), lr=1e-3)

        self.gamma = gamma
        self.tau = tau

        """
        Copy initial params of the actor and critic networks to their respective target networks
        """
        hard_update(self.actor_target, self.actor)  # Make sure target is with the same weight
        hard_update(self.critic_target, self.critic)


    def select_action(self, state, action_noise=None, param_noise=None):
        """
        Select action with non-target actor network and add actor noise for exploration
        """
        self.actor.eval() # https://stackoverflow.com/questions/48146926/whats-the-meaning-of-function-eval-in-torch-nn-module
        if param_noise is not None: 
            mu = self.actor_perturbed((Variable(state)))
        else:
            mu = self.actor((Variable(state)))

        self.actor.train() # switch back to training mode

        mu = mu.data

        if action_noise is not None:
            mu += torch.Tensor(action_noise.noise())

        return mu.clamp(-1, 1)

    # TODO: better name for this?
    def select_target_action(self, state):
        """
        Select action from target actor network (no action or parameter noise)
        """
        self.actor_target.eval()

        mu = self.actor_target((Variable(state)))

        self.actor_target.train()

        mu = mu.data

        return mu.clamp(-1, 1)


    def update_parameters(self, batch):
        state_batch = Variable(torch.cat(batch.state))
        action_batch = Variable(torch.cat(batch.action))
        reward_batch = Variable(torch.cat(batch.reward))
        mask_batch = Variable(torch.cat(batch.mask))
        next_state_batch = Variable(torch.cat(batch.next_state))
        
        """
        In DDPG, next-state Q values are calculated with the target value network and target policy network
        Once this is calculated, use Bellman equation to calculated updated Q value
        """
        next_action_batch = self.actor_target(next_state_batch)
        next_state_action_values = self.critic_target(next_state_batch, next_action_batch)

        reward_batch = reward_batch.unsqueeze(1)
        mask_batch = mask_batch.unsqueeze(1)
        # This is bellman equation
        expected_state_action_batch = reward_batch + (self.gamma * mask_batch * next_state_action_values)

        """
        Minimize MSE loss between updated Q value and original Q value
        """
        state_action_batch = self.critic((state_batch), (action_batch))

        value_loss = F.mse_loss(state_action_batch, expected_state_action_batch)
        self.critic_optim.zero_grad()
        value_loss.backward()
        self.critic_optim.step()

        """
        Maxmize expected return for policy function
        """
        policy_loss = -self.critic((state_batch),self.actor((state_batch)))

        self.actor_optim.zero_grad()

        policy_loss = policy_loss.mean()
        policy_loss.backward()
        self.actor_optim.step()

        """
        Update the target networks ()
        """
        soft_update(self.actor_target, self.actor, self.tau)
        soft_update(self.critic_target, self.critic, self.tau)

        return value_loss.item(), policy_loss.item()

    def perturb_actor_parameters(self, param_noise):
        """Apply parameter noise to actor model, for exploration"""
        hard_update(self.actor_perturbed, self.actor)
        params = self.actor_perturbed.state_dict()
        for name in params:
            if 'ln' in name: 
                pass 
            param = params[name]
            param += torch.randn(param.shape) * param_noise.current_stddev
    
    def save(self):
        """Save all networks, including non-target"""

        save_path = os.path.join("./trained_models", "ddpg")

        try:
            os.makedirs(save_path)
        except OSError:
            pass

        filetype = ".pt" # pytorch model
        torch.save(self.actor_target, os.path.join("./trained_models", "target_actor_model" + filetype))
        torch.save(self.critic_target, os.path.join("./trained_models", "target_critic_model" + filetype))
        torch.save(self.actor, os.path.join("./trained_models", "actor_model" + filetype))
        torch.save(self.critic, os.path.join("./trained_models", "critic_model" + filetype))

    def save_model(self, env_name, suffix=".pt", actor_path=None, critic_path=None):
        if not os.path.exists('trained_models/'):
            os.makedirs('trained_models/')

        if actor_path is None:
            actor_path = "models/ddpg_actor_{}_{}".format(env_name, suffix) 
        if critic_path is None:
            critic_path = "models/ddpg_critic_{}_{}".format(env_name, suffix) 
        print('Saving models to {} and {}'.format(actor_path, critic_path))
        torch.save(self.actor.state_dict(), actor_path)
        torch.save(self.critic.state_dict(), critic_path)

    def load_model(self, model_path):
        target_actor_path = os.path.join(model_path, "target_actor_model.pt")
        target_critic_path = os.path.join(model_path, "target_critic_model.pt")
        actor_path = os.path.join(model_path, "actor_model.pt")
        critic_path = os.path.join(model_path, "critic_model.pt")
        print('Loading models from {}, {}, {}, and {}'.format(target_actor_path, target_critic_path, actor_path, critic_path))
        if actor_path is not None:
            self.actor = torch.load(actor_path)
        if critic_path is not None: 
            self.critic = torch.load(critic_path)

    def train(self, env, memory, num_episodes, ounoise, param_noise, args, logger=None):

        # TODO add procedure to improve exploration at start of training: for fixed number of steps (hyperparam) have agent take actions sampled from uniform random distribution over valid actions.

        rewards = []
        total_numsteps = 0
        updates = 0

        start_time = time.time()

        for i_episode in range(args.num_episodes):
            print("********** Episode {} ************".format(i_episode))
            state = torch.Tensor([env.reset()])

            if args.ou_noise: 
                ounoise.scale = (args.noise_scale - args.final_noise_scale) * max(0, args.exploration_end -
                                                                            i_episode) / args.exploration_end + args.final_noise_scale
                ounoise.reset()

            if args.param_noise:
                self.perturb_actor_parameters(param_noise)

            episode_reward = 0
            episode_start = time.time()
            while True:
                """
                select action according to current policy and exploration noise
                """
                action = self.select_action(state, ounoise, param_noise)

                """
                execute action and observe reward and new state
                """
                next_state, reward, done, _ = env.step(action.numpy()[0])
                total_numsteps += 1
                episode_reward += reward

                action = torch.Tensor(action)
                mask = torch.Tensor([not done])
                next_state = torch.Tensor([next_state])
                reward = torch.Tensor([reward])

                """
                store transition tuple in replay buffer
                """
                memory.push(state, action, mask, next_state, reward)

                state = next_state

                if len(memory) > args.batch_size:
                    for _ in range(args.updates_per_step):
                        """
                        Sample random minibatch of (args.batch_size) transitions
                        """
                        transitions = memory.sample(args.batch_size)
                        batch = Transition(*zip(*transitions))

                        """
                        Calculate updated Q value (Bellman equation) and update parameters of all networks
                        """
                        value_loss, policy_loss = self.update_parameters(batch)

                        #writer.add_scalar('loss/value', value_loss, updates)
                        #writer.add_scalar('loss/policy', policy_loss, updates)

                        updates += 1
                if done:
                    break

            print("time elapsed: {:.2f} s".format(time.time() - start_time))
            print("episode time elapsed: {:.2f} s".format(time.time() - episode_start))

            #writer.add_scalar('reward/train', episode_reward, i_episode)

            # Update param_noise based on distance metric
            if args.param_noise:
                episode_transitions = memory.memory[memory.position-t:memory.position]
                states = torch.cat([transition[0] for transition in episode_transitions], 0)
                unperturbed_actions = self.select_action(states, None, None)
                perturbed_actions = torch.cat([transition[1] for transition in episode_transitions], 0)

                ddpg_dist = ddpg_distance_metric(perturbed_actions.numpy(), unperturbed_actions.numpy())
                param_noise.adapt(ddpg_dist)

            """
            Logging with visdom
            """
            if logger is not None:
                """
                Evaluate non-target actor
                """
                state = torch.Tensor([env.reset()])
                episode_reward = 0
                evaluate_start = time.time()
                while True:
                    action = self.select_action(state)

                    next_state, reward, done, _ = env.step(action.numpy()[0])
                    episode_reward += reward

                    next_state = torch.Tensor([next_state])

                    state = next_state
                    if done:
                        print("non-target evaluate time elapsed: {:.2f} s".format(time.time() - evaluate_start))
                        break

                rewards.append(episode_reward)
                logger.record("Return (non-target)", rewards[-1])

                """
                Repeat for target network
                """
                state = torch.Tensor([env.reset()])
                episode_reward = 0
                evaluate_start = time.time()
                while True:
                    action = self.select_target_action(state)

                    next_state, reward, done, _ = env.step(action.numpy()[0])
                    episode_reward += reward

                    next_state = torch.Tensor([next_state])

                    state = next_state
                    if done:
                        print("target evaluate time elapsed: {:.2f} s".format(time.time() - evaluate_start))
                        break

                rewards.append(episode_reward)
                logger.record("Return (target)", rewards[-1])


                logger.dump()


            if i_episode % 10 == 0:
                self.save()
            #     state = torch.Tensor([env.reset()])
            #     episode_reward = 0
            #     while True:
            #         action = self.select_action(state)

            #         next_state, reward, done, _ = env.step(action.numpy()[0])
            #         episode_reward += reward

            #         next_state = torch.Tensor([next_state])

            #         state = next_state
            #         if done:
            #             break

                #writer.add_scalar('reward/test', episode_reward, i_episode)
                print("Episode: {}, total numsteps: {}, reward: {}, average reward: {}".format(i_episode, total_numsteps, rewards[-1], np.mean(rewards[-10:])))
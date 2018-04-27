from abc import ABC, abstractmethod
import torch
import numpy

from torch_ac.format import default_preprocess_obss, default_reshape_reward
from torch_ac.utils import DictList, MultiEnv

class BaseAlgo(ABC):
    def __init__(self, envs, acmodel, frames_per_agent, discount, lr, gae_tau, entropy_coef,
                 value_loss_coef, max_grad_norm, preprocess_obss, reshape_reward):
        self.env = MultiEnv(envs)
        self.acmodel = acmodel
        self.acmodel.train()
        self.frames_per_agent = frames_per_agent
        self.discount = discount
        self.lr = lr
        self.gae_tau = gae_tau
        self.entropy_coef = entropy_coef
        self.value_loss_coef = value_loss_coef
        self.max_grad_norm = max_grad_norm
        self.preprocess_obss = preprocess_obss or default_preprocess_obss
        self.reshape_reward = reshape_reward or default_reshape_reward

        self.num_processes = len(envs)
        self.obs = self.env.reset()

        self.log_episode_return = numpy.zeros(self.num_processes)
        self.log_episode_reshaped_return = numpy.zeros(self.num_processes)
        self.log_episode_num_frames = numpy.zeros(self.num_processes)
        self.log_return = numpy.zeros(self.num_processes)
        self.log_reshaped_return = numpy.zeros(self.num_processes)
        self.log_num_frames = numpy.zeros(self.num_processes)
    
    def collect_transitions(self):
        ts = DictList()

        # Add obs, action, reward, mask and value to transitions

        for _ in range(self.frames_per_agent):
            # Do one agent-environment interaction

            preprocessed_obs = self.preprocess_obss(self.obs, use_gpu=torch.cuda.is_available())
            with torch.no_grad():
                action = self.acmodel.get_action(preprocessed_obs)
            action = action.cpu().numpy()
            obs, reward, done, _ = self.env.step(action)
            
            # Add a transition

            reshaped_reward = [
                self.reshape_reward(obs_, action_, reward_)
                for obs_, action_, reward_ in zip(obs, action, reward)
            ]
            mask = [0 if done_ else 1 for done_ in done]
            with torch.no_grad():
                value = self.acmodel.get_value(preprocessed_obs)
            value = value.squeeze(1).cpu().numpy()

            ts.append({"obs": self.obs, "action": action, "reward": reshaped_reward, "mask": mask, "value": value})
            
            # Update the observation

            self.obs = obs

            # Update log values

            mask = numpy.array(mask)

            self.log_episode_return += numpy.array(reward)
            self.log_episode_reshaped_return += numpy.array(reshaped_reward)
            self.log_episode_num_frames += numpy.ones(self.num_processes)

            self.log_return *= mask
            self.log_return += (1 - mask) * self.log_episode_return
            self.log_reshaped_return *= mask
            self.log_reshaped_return += (1 - mask) * self.log_episode_reshaped_return
            self.log_num_frames *= mask
            self.log_num_frames += (1 - mask) * self.log_episode_num_frames

            self.log_episode_return *= mask
            self.log_episode_reshaped_return *= mask
            self.log_episode_num_frames *= mask

        ts.action = torch.tensor(ts.action)
        ts.reward = torch.tensor(ts.reward).float()
        ts.mask = torch.tensor(ts.mask).float()
        ts.value = torch.tensor(ts.value).float()
        if torch.cuda.is_available():
            ts.action = ts.action.cuda()
            ts.reward = ts.reward.cuda()
            ts.mask = ts.mask.cuda()
            ts.value = ts.value.cuda()
        
        # Add advantage and return to transitions

        ts.advantage = torch.zeros(*ts.reward.shape).float()
        if torch.cuda.is_available():
            ts.advantage = ts.advantage.cuda()

        preprocessed_obs = self.preprocess_obss(self.obs, use_gpu=torch.cuda.is_available())
        with torch.no_grad():
            next_value = self.acmodel.get_value(preprocessed_obs).squeeze(1)

        for i in reversed(range(self.frames_per_agent)):
            next_value = ts.value[i+1] if i < self.frames_per_agent - 1 else next_value
            next_advantage = ts.advantage[i+1] if i < self.frames_per_agent - 1 else 0
            
            delta = ts.reward[i] + self.discount * next_value * ts.mask[i] - ts.value[i]
            ts.advantage[i] = delta + self.discount * self.gae_tau * next_advantage * ts.mask[i]

        ts.returnn = ts.advantage + ts.value

        # Reshape each transitions attribute

        ts.obs = [obs for obss in ts.obs for obs in obss]
        ts.action = ts.action.view(-1, *ts.action.shape[2:])
        ts.reward = ts.reward.view(-1, *ts.reward.shape[2:])
        ts.mask = ts.mask.view(-1, *ts.mask.shape[2:])
        ts.value = ts.value.view(-1, *ts.value.shape[2:])
        ts.advantage = ts.advantage.view(-1, *ts.advantage.shape[2:])
        ts.returnn = ts.returnn.view(-1, *ts.returnn.shape[2:])

        # Log some values

        log = {
            "return": self.log_return,
            "reshaped_return": self.log_reshaped_return,
            "total_num_frames": self.frames_per_update*self.num_processes,
            "num_frames": self.log_num_frames
        }

        return ts, log

    @abstractmethod
    def update_parameters(self):
        pass
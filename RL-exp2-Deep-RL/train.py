import os
import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np
import random
import gymnasium as gym
from torch.utils.tensorboard import SummaryWriter
import collections
import argparse
from tqdm import tqdm
import time
import copy

# Hyperparameters
EPISODES = 2000  # Number of training/testing episodes
BATCH_SIZE = 64
LR = 0.00025
GAMMA = 0.98
ACTION_DIM = 25
HIDDEN_DIM = 128
SAVING_IETRATION = 10000  # Interval for saving checkpoints
MEMORY_CAPACITY = 10000  # Capacity of replay memory
MIN_CAPACITY = 1024  # Minimum memory before learning starts
Q_NETWORK_ITERATION =50  # Interval for syncing target network
EPSILON = 0.01  # epsilon-greedy
SEED = 0
MODEL_PATH = ''
SAVE_PATH_PREFIX = './logs'
TEST = False  # Flag to control training or testing mode
ENV = 'CartPole'

# Choose an experimental environment
# Classic Control
# env = gym.make('CartPole-v1', render_mode="human" if TEST else None)
# env = gym.make('MountainCar-v0', render_mode="human" if TEST else None)
# ......
# LunarLander
# env = gym.make("LunarLander-v2", continuous=False, gravity=-10.0, enable_wind=True, wind_power=15.0, turbulence_power=1.5, render_mode="human" if TEST else None)

def get_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", '-e', type=str, default=ENV)
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--test", action="store_true")
    parser.add_argument("--model", type=str, default=MODEL_PATH)
    parser.add_argument("--save_path", type=str, default=SAVE_PATH_PREFIX)

    parser.add_argument("--episodes", '-n', type=int, default=EPISODES)
    parser.add_argument("--batch_size", '-b', type=int, default=BATCH_SIZE)
    parser.add_argument("--learning_rate", '-lr', type=float, default=LR)
    parser.add_argument("--scheduler", '-s', action="store_true")
    parser.add_argument("--patience", '-p', type=int, default=100)

    parser.add_argument("--hidden_dim", '-hd', type=int, default=HIDDEN_DIM)
    parser.add_argument("--action_dim", '-ad', type=int, default=ACTION_DIM)
    parser.add_argument("--gamma", '-g', type=float, default=GAMMA)
    parser.add_argument("--saving_iteration", '-si', type=int, default=SAVING_IETRATION)
    parser.add_argument("--memory_capacity", '-mc', type=int, default=MEMORY_CAPACITY)
    parser.add_argument("--min_capacity", '-minc', type=int, default=MIN_CAPACITY)
    parser.add_argument("--q_network_iteration", '-qi', type=int, default=Q_NETWORK_ITERATION)
    parser.add_argument("--epsilon", '-eps', type=float, default=EPSILON)

    parser.add_argument("--must-done", '-md', action="store_true")
    
    # New argument to choose the algorithm
    parser.add_argument("--algorithm", '-alg', type=str, choices=['DQN', 'DoubleDQN','DuelingDQN'], default='DQN', 
                        help="Choose from DQN, DoubleDQN, DuelingDQN algorithms")
    
    return parser.parse_args()

class Qnet(nn.Module):
    def __init__(self, num_inputs=4, hidden_dim=128, num_actions=2):
        # Input dimension is num_inputs, output dimension is num_actions
        super(Qnet, self).__init__()
        self.fc1 = nn.Linear(num_inputs, hidden_dim)
        self.fc2 = nn.Linear(hidden_dim, hidden_dim)
        self.fc3 = nn.Linear(hidden_dim, num_actions)

    def forward(self, x):
        x = F.relu(self.fc1(x))
        x = F.relu(self.fc2(x))
        x = self.fc3(x)
        return x
class VAnet(torch.nn.Module):
    def __init__(self, num_inputs, hidden_dim, num_actions):
        super(VAnet, self).__init__()
        self.fc1 = torch.nn.Linear(num_inputs, hidden_dim)
        self.fc2 = torch.nn.Linear(hidden_dim, hidden_dim)
        self.fc_A = torch.nn.Linear(hidden_dim, num_actions)
        self.fc_V = torch.nn.Linear(hidden_dim, 1)

    def forward(self, x):
        x = F.relu(self.fc1(x))
        x = F.relu(self.fc2(x))
        A = self.fc_A(x)
        V = self.fc_V(x)
        Q = V + A - A.mean(-1).view(-1, 1)
        return Q

class Data:

    def __init__(self, state, action, reward, next_state, done):
        self.state = state
        self.action = action
        self.reward = reward
        self.next_state = next_state
        self.done = done

class Memory:
    """Experience Replay Memory"""

    def __init__(self, capacity):
        self.buffer = collections.deque(maxlen=capacity)

    def set(self, data):
        self.buffer.append(data)

    def get(self, batch_size):
        batch = random.sample(self.buffer, batch_size)
        states = torch.tensor(np.array([data.state for data in batch]), dtype=torch.float)
        actions = torch.tensor(np.array([data.action for data in batch]), dtype=torch.long).unsqueeze(1)
        rewards = torch.tensor(np.array([data.reward for data in batch]), dtype=torch.float).unsqueeze(1)
        next_states = torch.tensor(np.array([data.next_state for data in batch]), dtype=torch.float)
        dones = torch.tensor(np.array([data.done for data in batch]), dtype=torch.float).unsqueeze(1)
        return states, actions, rewards, next_states, dones

class DQN():
    """Deep Q-Network"""

    def __init__(self, config, method='DQN'):
        super(DQN, self).__init__()
        self.device = config['device']
        self.num_actions = config['num_actions']
        self.num_states = config['num_states']
        self.save_path = config['save_path']
        self.q_network_iteration = config['q_network_iteration']
        self.saving_iteration = config['saving_iteration']
        self.gamma = config['gamma']
        self.method = method
        if method == 'duelingdqn':
            self.eval_net = VAnet(num_inputs=self.num_states,
                            hidden_dim=config['hidden_dim'], 
                            num_actions=self.num_actions).to(self.device)
            self.target_net = VAnet(num_inputs=self.num_states, 
                            hidden_dim=config['hidden_dim'], 
                            num_actions=self.num_actions).to(self.device)
        elif method == 'dqn' or method == 'doubledqn':
            self.eval_net = Qnet(num_inputs=self.num_states,
                                hidden_dim=config['hidden_dim'], 
                                num_actions=self.num_actions).to(self.device)
            self.target_net = Qnet(num_inputs=self.num_states, 
                                hidden_dim=config['hidden_dim'], 
                                num_actions=self.num_actions).to(self.device)
        else:
            raise ValueError("Please choose a valid algorithm: DQN, DoubleDQN, DuelingDQN")
        self.target_net.load_state_dict(self.eval_net.state_dict())
        self.target_net.eval()

        self.learn_step_counter = 0
        self.memory_counter = 0
        self.memory = Memory(config['memory_capacity'])
        self.optimizer = torch.optim.Adam(self.eval_net.parameters(), lr=config['learning_rate'])
        if config['scheduler']:
            self.scheduler = torch.optim.lr_scheduler.StepLR(self.optimizer, step_size=5000, gamma=0.95)
        else:
            self.scheduler = None
        self.loss_func = nn.MSELoss()

    def max_q_value(self, state):
        state = torch.tensor(state, dtype=torch.float).to(self.device)
        with torch.no_grad():
            action_value = self.eval_net.forward(state)
        return torch.max(action_value).item()

    def choose_action(self, state, EPSILON=0.01):
        state = torch.tensor(state, dtype=torch.float).to(self.device)

        if np.random.random() > EPSILON:  # Greedy policy
            with torch.no_grad():
                action_value = self.eval_net.forward(state)
            action = torch.argmax(action_value).item()
        else:
            # Random policy
            action = np.random.randint(0, self.num_actions)  # Random integer
        return action

    def store_transition(self, data):
        self.memory.set(data)
        self.memory_counter += 1

    def learn(self, BATCH_SIZE=BATCH_SIZE):
        # Update the target network
        if self.learn_step_counter % self.q_network_iteration == 0:
            self.target_net.load_state_dict(self.eval_net.state_dict())
        if self.learn_step_counter % self.saving_iteration == 0:
            self.save_train_model(self.learn_step_counter)

        self.learn_step_counter += 1

        # Sample a batch of transitions
        states, actions, rewards, next_states, dones = self.memory.get(BATCH_SIZE)

        states = states.to(self.device)
        actions = actions.to(self.device)
        rewards = rewards.to(self.device)
        next_states = next_states.to(self.device)
        dones = dones.to(self.device)

        # Current Q values
        q_eval = self.eval_net(states).gather(1, actions)

        # DQN: Action Selection and Evaluation using eval_net
        # Double DQN: Action Selection using eval_net, Action Evaluation using target_net
        # Dueling DQN: Action Evaluation using eval_net
        with torch.no_grad():
            if self.method == 'doubledqn':
                # Select the best action based on eval_net
                actions_eval = self.eval_net(next_states).argmax(1, keepdim=True)
                # Evaluate the selected actions using target_net
                q_next = self.target_net(next_states).gather(1, actions_eval)
            else:
                q_next = self.target_net(next_states).max(1, keepdim=True)[0]
            q_target = rewards + self.gamma * q_next * (1 - dones)

        # Compute loss
        loss = self.loss_func(q_eval, q_target)

        # Optimize the model
        self.optimizer.zero_grad()
        loss.backward()
        self.optimizer.step()
        if self.scheduler:
            self.scheduler.step()
        return loss.item()

    def save_train_model(self, epoch):
        ckpt_path = os.path.join(self.save_path, 'ckpt')
        os.makedirs(ckpt_path, exist_ok=True)
        torch.save(self.eval_net.state_dict(),
                     os.path.join(ckpt_path, f"{epoch}.pth"))


    def load_net(self, eval_file):
        self.eval_net.load_state_dict(torch.load(eval_file, map_location=self.device, weights_only=True))
        self.target_net.load_state_dict(torch.load(eval_file, map_location=self.device, weights_only=True))


def dis_to_con(discrete_action, env, num_actions):
    action_lowbound = env.action_space.low[0]
    action_upbound = env.action_space.high[0]
    return action_lowbound + (discrete_action /
                            (num_actions - 1)) * (action_upbound -
                                                    action_lowbound)

def main():
    args = get_args()
    if args.test:
        args.episodes = 1
    timenow = time.strftime("%Y-%m-%d-%H-%M", time.localtime())
    save_path = os.path.join(args.save_path, args.env, args.algorithm, timenow)

    random.seed(args.seed)
    torch.manual_seed(args.seed)
    np.random.seed(args.seed)

    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

    # Create environment
    if args.env == 'CartPole':
        env = gym.make('CartPole-v1', render_mode="human" if args.test else None)
    elif args.env == "Pendulum":
        env = gym.make("Pendulum-v1", g=9.81, render_mode="human" if args.test else None)
    elif args.env == 'Acrobot':
        env = gym.make('Acrobot-v1', render_mode="human" if args.test else None)
    elif args.env == 'MountainCar':
        env = gym.make("MountainCarContinuous-v0", render_mode="human" if args.test else None)
    elif args.env == 'LunarLander':
        env = gym.make("LunarLander-v3", continuous=False, gravity=-10.0, enable_wind=True, wind_power=15.0, turbulence_power=1.5, render_mode="human" if args.test else None)
    else:
        assert False, "Please choose a valid environment: CartPole-v1, Acrobot-v1, LunarLander-v3"

    is_discrete = isinstance(env.action_space, gym.spaces.Discrete)
    num_actions = env.action_space.n  if is_discrete else args.action_dim
    num_states = env.observation_space.shape[0]  # e.g., 4 for CartPole-v1


    config = {
        'num_actions': num_actions,
        'num_states': num_states,
        'device': device,
        'memory_capacity': args.memory_capacity,
        'learning_rate': args.learning_rate,
        'scheduler': args.scheduler,
        'save_path': save_path,
        'q_network_iteration': args.q_network_iteration,
        'saving_iteration': args.saving_iteration,
        'gamma': args.gamma,
        'hidden_dim': args.hidden_dim,
    }

    # Instantiate the chosen algorithm
    algorithm = args.algorithm.lower()
    agent = DQN(config, method=algorithm)
    print(f"Using {algorithm} Algorithm")

    if args.test:
        if args.model == '':
            raise ValueError("Please provide a model path for testing using --model")
        agent.load_net(args.model)

    max_q_value  = 0
    first_done = False
    writer = None if args.test else SummaryWriter(save_path)
    with tqdm(range(args.episodes)) as pbar:
        best_reward = -np.inf
        early_stopping = 0
        for i in range(args.episodes):
            if early_stopping == args.patience:
                print(f"Early Stopping at Episode {i}, Best Reward: {best_reward}")
                break
            # print(f"EPISODE: {i+1}/{args.episodes}")
            state, info = env.reset(seed=args.seed)
            state = np.array(state)  # Ensure state is a NumPy array
            ep_reward = 0
            loss = 0
            count = 0
            while True:
                action = agent.choose_action(
                    state=state,
                    EPSILON=args.epsilon if not args.test else 0)  # choose best action
                max_q_value = agent.max_q_value(state) * 0.005 + 0.995 * max_q_value

                if is_discrete:
                    next_state, reward, done, truncated, info = env.step(action)
                else:
                    con_action = dis_to_con(action, env, num_actions)
                    next_state, reward, done, truncated, info = env.step([con_action])  # observe next state and reward
                    
                agent.store_transition(Data(state, action, reward, next_state, done))
                ep_reward = ep_reward + reward
                if args.test:
                    env.render()
                truncated = truncated and (not args.must_done)
                if done or truncated:
                    if args.test:
                        pbar.set_postfix({'Test Reward': round(ep_reward, 3)})
                    else:
                        if not first_done or done:
                            if ep_reward > best_reward:
                                best_reward = copy.deepcopy(ep_reward)
                                early_stopping = 0
                                agent.save_train_model(f"best")
                                print(f"Best Reward at Episode {i}, Reward: {ep_reward}")
                        pbar.set_postfix({'Loss': loss / count if count != 0 else 0,
                                        'Reward': round(ep_reward, 3),
                                        'Max Q Value': max_q_value,      
                                        'Best Reward': round(best_reward, 3)})
                    if done and not first_done:
                        first_done = True
                        print(f"Fist Done at Episode {i}, Reward: {ep_reward}")
                    break
                
                if agent.memory_counter >= args.min_capacity and not args.test:
                    loss += agent.learn(BATCH_SIZE=args.batch_size)
                    count += 1
                state = next_state
            pbar.update(1)
            early_stopping += 1
            if writer:
                writer.add_scalar('Reward', ep_reward, global_step=i)
                writer.add_scalar('Loss', loss / count if count != 0 else 0, global_step=i)
                writer.add_scalar('Max Q Value', max_q_value, global_step=i)
                if early_stopping == 1:
                    writer.add_scalar('Best Reward', best_reward, global_step=i)
    agent.save_train_model("final")
    env.close()
    if writer:
        writer.add_hparams(vars(args), {'hparam/Reward': best_reward})
        writer.close()

if __name__ == '__main__':
    main()

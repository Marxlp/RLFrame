import torch
import numpy as np
from torch.optim.adam import Adam
from torch.optim.lbfgs import LBFGS
from core.model.policy_with_value import PolicyWithValue
from core.common import StepDictList, ParamDict


def update_value_net(value_net, states, returns, l2_reg):
    optimizer = LBFGS(value_net.parameters(), max_iter=25, history_size=5)

    def closure():
        optimizer.zero_grad()
        values_pred = value_net(states)
        value_loss = (values_pred - returns).pow(2).mean()

        # weight decay
        for param in value_net.parameters():
            value_loss += param.pow(2).sum() * l2_reg
        value_loss.backward()
        return value_loss

    optimizer.step(closure)


def get_tensor(policy, reply_memory, device):
    policy.estimate_advantage(reply_memory)
    states = []
    actions = []
    advantages = []
    returns = []

    for b in reply_memory:
        advantages.extend([[tr["advantage"]] for tr in b["trajectory"]])
        returns.extend([[tr["return"]] for tr in b["trajectory"]])
        states.extend([tr['s'] for tr in b["trajectory"]])
        actions.extend([tr['a'] for tr in b["trajectory"]])

    states = torch.as_tensor(states, dtype=torch.float32, device=device)
    actions = torch.as_tensor(actions, dtype=torch.float32, device=device)
    advantages = torch.as_tensor(advantages, dtype=torch.float32, device=device)
    returns = torch.as_tensor(returns, dtype=torch.float32, device=device)
    return states, actions, advantages, returns


def ppo_step(config: ParamDict, replay_memory: StepDictList, policy: PolicyWithValue):
    lr, l2_reg, clip_epsilon, policy_iter, i_iter, max_iter, mini_batch_sz = \
        config.require("lr", "l2 reg", "clip eps", "optimize policy epochs",
                       "current training iter", "max iter", "optimize batch size")
    lam_entropy = 0.0
    states, actions, advantages, returns = get_tensor(policy, replay_memory, policy.device)

    """update critic"""
    update_value_net(policy.value_net, states, returns, l2_reg)

    """update policy"""
    lr_mult = max(1.0 - i_iter / max_iter, 0.)
    clip_epsilon = clip_epsilon * lr_mult
    optimizer = Adam(policy.policy_net.parameters(), lr=lr * lr_mult, weight_decay=l2_reg)

    with torch.no_grad():
        fixed_log_probs = policy.policy_net.get_log_prob(states, actions).detach()

    inds = torch.arange(states.size(0))

    for _ in range(policy_iter):
        np.random.shuffle(inds)

        """perform mini-batch PPO update"""
        for i_b in range(int(np.ceil(states.size(0) / mini_batch_sz))):
            ind = inds[i_b * mini_batch_sz: min((i_b+1) * mini_batch_sz, inds.size(0))]

            log_probs, entropy = policy.policy_net.get_log_prob_entropy(states[ind], actions[ind])
            ratio = torch.exp(log_probs - fixed_log_probs[ind])
            surr1 = ratio * advantages[ind]
            surr2 = torch.clamp(ratio, 1.0 - clip_epsilon, 1.0 + clip_epsilon) * advantages[ind]
            policy_surr = -torch.min(surr1, surr2).mean()
            policy_surr -= entropy.mean() * lam_entropy
            optimizer.zero_grad()
            policy_surr.backward()
            torch.nn.utils.clip_grad_norm_(policy.policy_net.parameters(), 0.5)
            optimizer.step()

import math
from abc import ABC, abstractmethod
from typing import Union, Optional, List

import torch
from torch import Tensor


def _get_arm_indices(
    ids_of_all_arms: List[Union[str, int]], ids_of_arms_in_batch: List[Union[str, int]]
) -> List[int]:
    arm_idxs = []
    for i in ids_of_arms_in_batch:
        try:
            arm_idxs.append(ids_of_all_arms.index(i))
        except ValueError:
            raise ValueError(f"Unknown arm_id {i}. Known arm ids: {ids_of_all_arms}")
    return arm_idxs


def _place_values_at_indices(values: Tensor, idxs: List[int], total_len: int) -> Tensor:
    """

    TODO: maybe replace with sparse vector function?

    Args:
        values (Tensor): The values
        idxs (List[int]): The indices at which the values have to be placed
        total_len (int): Length of the array
    """
    assert len(values) == len(idxs)
    ret = torch.zeros(total_len)
    ret[idxs] = values
    return ret


class BaseUCB(torch.nn.Module, ABC):
    """
    Base class for UCB-like Multi-Armed Bandits (MAB)
    """

    def __init__(
        self,
        *,
        n_arms: Optional[int] = None,
        arm_ids: Optional[List[Union[str, int]]] = None,
    ):
        super().__init__()
        if n_arms is not None:
            self.arm_ids = list(range(n_arms))
            self.n_arms = n_arms
        if arm_ids is not None:
            self.arm_ids = arm_ids
            self.n_arms = len(arm_ids)
        self.total_n_obs_all_arms = 0
        self.total_n_obs_per_arm = torch.zeros(self.n_arms)
        self.total_sum_reward_per_arm = torch.zeros(self.n_arms)

    def add_batch_observations(
        self,
        n_obs_per_arm: Tensor,
        sum_reward_per_arm: Tensor,
        arm_ids: Optional[List[Union[str, int]]] = None,
    ):
        if not isinstance(n_obs_per_arm, Tensor):
            n_obs_per_arm = torch.tensor(n_obs_per_arm, dtype=torch.float)
        if not isinstance(sum_reward_per_arm, Tensor):
            sum_reward_per_arm = torch.tensor(sum_reward_per_arm, dtype=torch.float)
        if arm_ids is None or arm_ids == self.arm_ids:
            # assume that the observations are for all arms in the default order
            arm_ids = self.arm_ids
            arm_idxs = list(range(self.n_arms))
        else:
            assert len(arm_ids) == len(
                set(arm_ids)
            )  # make sure no duplicates in arm IDs

            # get the indices of the arms
            arm_idxs = _get_arm_indices(self.arm_ids, arm_ids)

            # put elements from the batch in the positions specified by `arm_ids` (missing arms will be zero)
            n_obs_per_arm = _place_values_at_indices(
                n_obs_per_arm, arm_idxs, self.n_arms
            )
            sum_reward_per_arm = _place_values_at_indices(
                sum_reward_per_arm, arm_idxs, self.n_arms
            )

        self.total_n_obs_per_arm += n_obs_per_arm
        self.total_sum_reward_per_arm += sum_reward_per_arm
        self.total_n_obs_all_arms += int(n_obs_per_arm.sum())

    def add_single_observation(self, arm_id: int, reward: float):
        assert arm_id in self.arm_ids
        arm_idx = self.arm_ids.index(arm_id)
        self.total_n_obs_per_arm[arm_idx] += 1
        self.total_sum_reward_per_arm[arm_idx] += reward
        self.total_n_obs_all_arms += 1

    def get_avg_reward_values(self) -> Tensor:
        return self.total_sum_reward_per_arm / self.total_n_obs_per_arm

    def get_action(self) -> Union[str, int]:
        """
        Get the id of the action chosen by the UCB algorithm

        Returns:
            int: The integer ID of the chosen action
        """
        ucb_scores = self.get_ucb_scores()
        return self.arm_ids[torch.argmax(ucb_scores)]

    @classmethod
    def get_ucb_scores_from_batch(
        cls,
        n_obs_per_arm: Tensor,
        sum_reward_per_arm: Tensor,
        *args,
        **kwargs,
    ) -> Tensor:
        """
        A utility method used to create the bandit, feed in a batch of observations and get the UCB scores in one function call

        Args:
            n_obs_per_arm (Tensor): An array of counts of per-arm numbers of observations
            sum_reward_per_arm (Tensor): An array of sums of rewards for each arm
            (additional arguments can be provided for specific concrete class implementations)

        Returns:
            Tensor: Array of per-arm UCB scores
        """
        n_arms = len(n_obs_per_arm)
        b = cls(n_arms=n_arms)
        b.add_batch_observations(n_obs_per_arm, sum_reward_per_arm, *args, **kwargs)
        return b.get_ucb_scores()

    @abstractmethod
    def get_ucb_scores(self):
        pass

    def __repr__(self):
        t = ", ".join(
            f"{v:.3f} ({int(n)})"
            for v, n in zip(self.get_avg_reward_values(), self.total_n_obs_per_arm)
        )
        return f"UCB({self.n_arms} arms; {t}"

    def forward(self):
        return self.get_ucb_scores()


class UCB1(BaseUCB):
    """
    Canonical implementation of UCB1
    Reference: https://www.cs.bham.ac.uk/internal/courses/robotics/lectures/ucb1.pdf
    """

    def get_ucb_scores(self):
        """
        Get per-arm UCB scores. The formula is
        UCB_i = AVG([rewards_i]) + SQRT(2*LN(T)/N_i)

        Returns:
            Tensor: An array of UCB scores (one per arm)
        """
        avg_rewards = self.get_avg_reward_values()
        log_t_over_ni = (
            math.log(self.total_n_obs_all_arms + 1) / self.total_n_obs_per_arm
        )
        ucb = avg_rewards + torch.sqrt(2 * log_t_over_ni)
        return torch.where(
            self.total_n_obs_per_arm > 0,
            ucb,
            torch.tensor(torch.inf, dtype=torch.float),
        )


class UCBTuned(BaseUCB):
    """
    Implementation of the UCB-Tuned algorithm from Section 4 of  https://link.springer.com/content/pdf/10.1023/A:1013689704352.pdf
    Biggest difference from basic UCB is that per-arm reward variance is estimated.
    """

    # _fields_for_saving = BaseUCB._fields_for_saving + [
    #     "total_sum_reward_squared_per_arm"
    # ]

    def __init__(
        self,
        n_arms: Optional[int] = None,
        arm_ids: Optional[List[Union[str, int]]] = None,
    ):
        super(UCBTuned, self).__init__(n_arms=n_arms, arm_ids=arm_ids)
        self.total_sum_reward_squared_per_arm = torch.zeros(self.n_arms)

    def add_batch_observations(
        self,
        n_obs_per_arm: Tensor,
        sum_reward_per_arm: Tensor,
        sum_reward_squared_per_arm: Tensor,
        arm_ids: Optional[List[Union[str, int]]] = None,
    ):
        """
        Add information about arm rewards in a batched form.

        Args:
            n_obs_per_arm (Tensor): An array of counts of per-arm numbers of observations
            sum_reward_per_arm (Tensor): An array of sums of rewards for each arm
            sum_reward_squared_per_arm (Tensor): An array of sums of squares of rewards for each arm
            arm_ids (Optional[List[Union[str, int]]]): A list of ids of arms in the same order as the elements of previous arrays
        """
        assert len(sum_reward_per_arm) == len(sum_reward_squared_per_arm)
        super().add_batch_observations(
            n_obs_per_arm, sum_reward_per_arm, arm_ids=arm_ids
        )
        if not isinstance(sum_reward_per_arm, Tensor):
            sum_reward_squared_per_arm = torch.tensor(
                sum_reward_squared_per_arm, dtype=torch.float
            )

        if arm_ids is None or arm_ids == self.arm_ids:
            # assume that the observations are for all arms in the default order
            arm_ids = self.arm_ids
            arm_idxs = list(range(self.n_arms))
        else:
            assert len(arm_ids) == len(
                set(arm_ids)
            )  # make sure no duplicates in arm IDs

            # get the indices of the arms
            arm_idxs = _get_arm_indices(self.arm_ids, arm_ids)

            # put elements from the batch in the positions specified by `arm_ids` (missing arms will be zero)
            sum_reward_squared_per_arm = _place_values_at_indices(
                sum_reward_squared_per_arm, arm_idxs, self.n_arms
            )

        self.total_sum_reward_squared_per_arm += sum_reward_squared_per_arm

    def add_single_observation(self, arm_id: int, reward: float):
        """
        Add a single observation (arm played, reward) to the bandit

        Args:
            arm_id (int): Which arm was played
            reward (float): Reward renerated by the arm
        """
        super().add_single_observation(arm_id, reward)
        arm_idx = self.arm_ids.index(arm_id)
        self.total_sum_reward_squared_per_arm[arm_idx] += reward ** 2

    def get_ucb_scores(self) -> Tensor:
        """
        Get per-arm UCB scores. The formula is
        UCB_i = AVG([rewards_i]) + SQRT(LN(T)/N_i * V_i)
        where V_i is a conservative variance estimate of arm i:
            V_i = AVG([rewards_i**2]) - AVG([rewards_i])**2 + sqrt(2ln(t) / n_i)
        Nore that we don't apply the min(1/4, ...) operator to the variance because this bandit is meant for non-Bernoulli applications as well

        Returns:
            Tensor: An array of UCB scores (one per arm)
        """
        avg_rewards = self.get_avg_reward_values()
        log_t_over_ni = (
            math.log(self.total_n_obs_all_arms + 1) / self.total_n_obs_per_arm
        )
        per_arm_var_est = (
            self.total_sum_reward_squared_per_arm / self.total_n_obs_per_arm
            - avg_rewards ** 2
            + torch.sqrt(
                2 * log_t_over_ni
            )  # additional term to make the estimate conservative (unlikely to underestimate)
        )
        ucb = avg_rewards + torch.sqrt(log_t_over_ni * per_arm_var_est)
        return torch.where(
            self.total_n_obs_per_arm > 0,
            ucb,
            torch.tensor(torch.inf, dtype=torch.float),
        )


class UCBTunedBernoulli(UCBTuned):
    def add_batch_observations(
        self,
        n_obs_per_arm: Tensor,
        num_success_per_arm: Tensor,
        arm_ids: Optional[List[Union[str, int]]] = None,
    ):
        """
        Add a batch of observations to the UCBTuned bandit, assuming Bernoulli distribution of rewards.
        Because of the Bernoulli assumption, we don't need to provide the squared rewards separately

        Args:
            n_obs_per_arm (Tensor): An array of counts of per-arm numbers of observations
            num_success_per_arm (Tensor): An array of counts of per-arm numbers of successes
        """
        super().add_batch_observations(
            n_obs_per_arm, num_success_per_arm, num_success_per_arm, arm_ids=arm_ids
        )


class MetricUCB(BaseUCB):
    """
    This is an improvement over UCB1 which uses a more precise confidence radius, especially for small expected rewards.
    Reference: https://arxiv.org/pdf/0809.4882.pdf
    """

    def get_ucb_scores(self):
        """
        Get per-arm UCB scores. The formula is
        UCB_i = AVG([rewards_i]) + SQRT(AVG([rewards_i]) * LN(T+1)/N_i) + LN(T+1)/N_i

        Returns:
            Tensor: An array of UCB scores (one per arm)
        """
        avg_rewards = self.get_avg_reward_values()
        log_t_over_ni = (
            math.log(self.total_n_obs_all_arms + 1) / self.total_n_obs_per_arm
        )
        ucb = avg_rewards + torch.sqrt(avg_rewards * log_t_over_ni) + log_t_over_ni
        return torch.where(
            self.total_n_obs_per_arm > 0,
            ucb,
            torch.tensor(torch.inf, dtype=torch.float),
        )


def get_bernoulli_tuned_ucb_scores(n_obs_per_arm, num_success_per_arm):
    # a minimalistic function that implements Tuned UCB for Bernoulli bandit
    avg_rewards = n_obs_per_arm / num_success_per_arm
    log_t_over_ni = torch.log(torch.sum(n_obs_per_arm)) / num_success_per_arm
    per_arm_var_est = (
        avg_rewards
        - avg_rewards ** 2
        + torch.sqrt(
            2 * log_t_over_ni
        )  # additional term to make the estimate conservative (unlikely to underestimate)
    )
    return avg_rewards + torch.sqrt(log_t_over_ni * per_arm_var_est)

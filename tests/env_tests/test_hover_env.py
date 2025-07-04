from typing import Any
from unittest.mock import Mock, patch

import numpy as np
import pytest
import torch

from data_driven_quad_control.envs.hover_env import HoverEnv
from data_driven_quad_control.envs.hover_env_config import EnvActionType


@pytest.mark.integration
@pytest.mark.drone_env_integration
def test_hover_env_loop(
    dummy_env_cfg: dict[str, Any],
    dummy_obs_cfg: dict[str, Any],
    dummy_reward_cfg: dict[str, Any],
    dummy_command_cfg: dict[str, Any],
) -> None:
    # Note: Genesis initialized in `tests/conftest.py`

    # Initialize environment
    num_envs = 2
    env = HoverEnv(
        num_envs=num_envs,
        env_cfg=dummy_env_cfg,
        obs_cfg=dummy_obs_cfg,
        reward_cfg=dummy_reward_cfg,
        command_cfg=dummy_command_cfg,
        show_viewer=False,
        device="cpu",
        action_type=EnvActionType.CTBR_FIXED_YAW,
    )

    # Reset environment
    obs, _ = env.reset()
    assert obs.shape == (num_envs, env.num_obs)

    # Step environment
    num_steps = 5
    with torch.no_grad():
        for _ in range(num_steps):
            dummy_actions = torch.zeros(
                (num_envs, env.num_actions),
                dtype=torch.float,
                device=env.device,
            )
            obs, reward, done, info = env.step(dummy_actions)

            assert obs.shape == (num_envs, env.num_obs)
            assert reward.shape == (num_envs,)
            assert done.shape == (num_envs,)
            assert isinstance(info, dict)

            # Check that rewards do not contain NaNs
            assert not torch.isnan(reward).any(), "Reward contains NaNs"

    # Sanity check reward keys
    for k in dummy_reward_cfg["reward_scales"].keys():
        assert f"rew_{k}" in env.extras["episode"]


@pytest.mark.parametrize("require_stabilization", [True, False])
def test_env_hovering_at_target(
    require_stabilization: bool,
    mock_env: HoverEnv,
) -> None:
    # Define test parameters
    num_envs = 2
    mock_env.hover_counter = torch.zeros(
        (num_envs,), device=mock_env.device, dtype=torch.int
    )
    mock_env.at_target_threshold = 1.0
    mock_env.rel_pos = torch.tensor(
        [
            [0.0, 0.0, 0.0],  # Drone at target
            [10.0, 10.0, 10.0],  # Drone far from target
        ]
    )
    mock_env.min_hover_steps = 2 if require_stabilization else 0

    # Test `HoverEnv._hovering_at_target`
    if require_stabilization:
        # First call:
        # Only update the hover counter of drones at target
        stabilized_at_target = HoverEnv._hovering_at_target(mock_env)

        assert stabilized_at_target.numel() == 0
        assert mock_env.hover_counter.tolist() == [1, 0]

        # Second call:
        # Update the hover counter and return the indices of drones that have
        # been at target for at least `min_hover_steps` (drone idx 0)
        stabilized_at_target = HoverEnv._hovering_at_target(mock_env)

        assert mock_env.hover_counter.tolist() == [2, 0]
        assert torch.equal(stabilized_at_target, torch.tensor([0]))
    else:
        at_target = HoverEnv._hovering_at_target(mock_env)

        # Verify that drone indices are returned
        # immediately when they reach the target
        assert torch.equal(at_target, torch.tensor([0]))


@pytest.mark.parametrize("add_obs_noise", [True, False])
def test_env_compute_observations(
    add_obs_noise: bool, mock_env: HoverEnv
) -> None:
    # Notes:
    # - `mock_env._add_noise` adds the `obs_noise_std` value to tensors
    #    as a constant "noise" to simplify testing.
    # - `mock_env.obs_scales` are all set to 1.0. This removes observation
    #   scaling and simplifies clipping, as mocked observations are in
    #   the [-1, 1] range.

    # Mock observation noise as a fixed constant
    if add_obs_noise:
        mock_env.obs_noise_std = 0.1
        mocked_noise = 0.1
    else:
        mocked_noise = 0.0

    # Construct expected observation
    pos = mock_env.rel_pos + mocked_noise
    quat = mock_env.base_quat + mocked_noise
    quat = quat / quat.norm(dim=1, keepdim=True)
    lin_vel = mock_env.base_lin_vel + mocked_noise
    ang_vel = mock_env.base_ang_vel + mocked_noise
    last_actions = mock_env.last_actions

    expected_obs = torch.cat(
        [pos, quat, lin_vel, ang_vel, last_actions], dim=-1
    )

    # Test `HoverEnv.compute_observations`
    obs = HoverEnv.compute_observations(mock_env)

    # Verify that the computed observation matches the expected tensor
    torch.testing.assert_close(obs, expected_obs)


@pytest.mark.parametrize("add_noise", [True, False])
@patch("torch.randn_like")
def test_env_get_pos_quat(
    mock_randn: Mock, add_noise: bool, mock_env: HoverEnv
) -> None:
    # Patch `torch.randn_like()` to `torch.ones_like`
    mock_randn.side_effect = lambda x: torch.ones_like(x)

    # Mock observation noise as a fixed constant
    mock_env.obs_noise_std = 0.1 if add_noise else 0.0

    # Construct expected observations
    if add_noise:
        expected_pos = (
            mock_env.base_pos
            + mock_env.obs_noise_std / mock_env.obs_scales["rel_pos"]
        )
    else:
        expected_pos = mock_env.base_pos

    pos = HoverEnv.get_pos(mock_env, add_noise)

    # Verify that the computed position matches the expected tensor
    torch.testing.assert_close(pos, expected_pos)


@pytest.mark.parametrize("add_noise", [True, False])
def test_env_get_quat(add_noise: bool, mock_env: HoverEnv) -> None:
    # Mock observation noise as a fixed constant
    mock_env.obs_noise_std = 0.1 if add_noise else 0.0

    # Construct expected observations
    if add_noise:
        expected_quat = mock_env.base_quat + mock_env.obs_noise_std
        expected_quat = expected_quat / expected_quat.norm(dim=1, keepdim=True)
    else:
        expected_quat = mock_env.base_quat

    quat = HoverEnv.get_quat(mock_env, add_noise)

    # Verify computed quaternion matches the expected tensor
    torch.testing.assert_close(quat, expected_quat)


@pytest.mark.parametrize("valid_color_list", [True, False])
def test_initialize_debug_drone_colors(
    valid_color_list: bool,
    mock_env: HoverEnv,
) -> None:
    # Define test parameters
    num_envs = 3
    dummy_color = (1.0, 0.0, 0.0, 1.0)
    test_drone_colors = (
        [dummy_color] * num_envs
        if valid_color_list
        else [dummy_color] * (num_envs - 1)
    )

    # Override number of environments in the mocked env
    mock_env.num_envs = num_envs

    # Mock the `scene` attribute in the mocked env
    mock_env.scene = Mock()
    mock_env.scene.envs_offset = np.zeros((num_envs, 3))

    # Test initialization function based on
    # whether the color list is valid or not
    if valid_color_list:
        HoverEnv.initialize_debug_color_spheres(mock_env, test_drone_colors)

        # Verify that debug color spheres are correctly enabled
        assert mock_env.drone_offsets.shape == (num_envs, 3)
        assert mock_env.drone_colors_enabled
        assert mock_env.drone_colors == test_drone_colors
    else:
        # Verify that a `ValueError` exception is raised
        # with invalid color lists
        with pytest.raises(ValueError, match="The number of colors"):
            HoverEnv.initialize_debug_color_spheres(
                mock_env, test_drone_colors
            )


def test_draw_colored_spheres(mock_env: HoverEnv) -> None:
    # Define test parameters
    num_envs = 3
    dummy_color = (1.0, 0.0, 0.0, 1.0)
    dummy_drone_colors = [dummy_color] * num_envs
    test_local_pos = torch.zeros((num_envs, 3))

    # Override number of environments in the mocked env
    mock_env.num_envs = num_envs
    mock_env.drone_colors = dummy_drone_colors

    # Mock the `scene` attribute in the mocked env
    mock_env.scene = Mock()

    # Mock `drone.get_pos()` in the mocked env
    mock_env.drone = Mock()
    mock_env.drone.get_pos.return_value = test_local_pos

    # Add test attributes to the mocked env
    mock_env.drone_offsets = torch.ones((num_envs, 3))

    # Call method
    HoverEnv.draw_colored_spheres(mock_env)

    # Verify scene methods were called the expected number of times
    mock_env.scene.clear_debug_objects.assert_called_once()
    assert mock_env.scene.draw_debug_sphere.call_count == num_envs

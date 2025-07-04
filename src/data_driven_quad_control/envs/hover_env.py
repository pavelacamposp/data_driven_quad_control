# Drone Environment based on the `hover_env.py` example
# from the Genesis repository.
#
# Repository: Genesis-Embodied-AI/Genesis
# URL: https://github.com/Genesis-Embodied-AI/Genesis
# License: Apache License 2.0 (See `LICENSE-APACHE` for details)
#
# Key modifications:
#   - Implemented a Collective Thrust and Body Rates (CTBR) internal
#     controller to support actions consisting of total thrust and body rates.
#   - Implemented action decimation (i.e., number of simulation steps
#     to take for each task step).
#   - Added early termination when excessive linear or angular velocities are
#     encountered to prevent numerical instabilities during simulation.
#   - Implemented methods for saving (`get_current_state`) and restoring
#     (`restore_from_state`) environment states.
#   - Added a parameter (`auto_target_updates`) to disable automatic target
#     position (command) updates.
#   - Integrated stochastic actuator and observation noise.
#   - Updated environment for compatibility with `rsl_rl_lib` v2.3.1.
#   - Added a configuration parameter (`env_cfg["min_hover_time_s"]`) to
#     define the minimum time (in seconds) a drone needs to hover at a target
#     before it is updated.
#   - Added a hover time reward (`_reward_hover_time`) to encourage drones to
#     stabilize at targets.
#   - Added a target closeness reward (`_reward_closeness`) to encourage
#     drones to approach targets when within their vicinity.
#   - Added a parameter (`drone_colors`) to enable drawing debug colored
#     spheres over drones for visual differentiation.
#   - Added methods to start video recording (`start_recording`) and to stop
#     and save the recording as a video file (`stop_and_save_recording`).
#   - Integrated camera rendering into simulation steps to simplify video
#     recording.
#   - Added a parameter (`camera_config`) to configure camera resolution,
#     position, look-at target, and FOV.

import math
from typing import Any

import genesis as gs
import torch
from genesis.engine.entities.drone_entity import DroneEntity
from genesis.engine.entities.rigid_entity import RigidEntity
from genesis.utils.geom import (
    inv_quat,
    quat_to_xyz,
    transform_by_quat,
    transform_quat_by_quat,
)

from data_driven_quad_control.controllers.ctbr.ctbr_controller import (
    DroneCTBRController,
)
from data_driven_quad_control.utilities.math_utils import (
    gs_rand_float,
    linear_interpolate,
)

from .hover_env_config import (
    EnvActionBounds,
    EnvActionType,
    EnvCTBRControllerConfig,
    EnvDroneParams,
    EnvState,
)


class HoverEnv:
    def __init__(
        self,
        num_envs: int,
        env_cfg: dict[str, Any],
        obs_cfg: dict[str, Any],
        reward_cfg: dict[str, Any],
        command_cfg: dict[str, Any],
        show_viewer: bool = False,
        device: torch.device | str = "cuda",
        auto_target_updates: bool = True,
        action_type: EnvActionType = EnvActionType.CTBR,
        drone_colors: list[tuple[float, float, float, float]] | None = None,
        camera_config: dict[str, Any] | None = None,
    ):
        self._is_closed = False  # Env closing status

        self.device = torch.device(device)

        self.dt = env_cfg["dt"]
        self.decimation = env_cfg["decimation"]
        self.step_dt = self.dt * self.decimation

        self.num_envs = num_envs
        self.action_type = action_type
        self.num_obs = EnvActionType.get_num_obs(self.action_type)
        self.num_actions = EnvActionType.get_num_actions(self.action_type)

        # Create CTBR controller if the action type is CTBR or CTBR_FIXED_YAW
        self.uses_ctbr_actions = self.action_type in (
            EnvActionType.CTBR,
            EnvActionType.CTBR_FIXED_YAW,
        )

        if self.uses_ctbr_actions:
            drone_params = EnvDroneParams.get()
            controller_config = EnvCTBRControllerConfig.get()
            self.ctbr_controller = DroneCTBRController(
                drone_params=drone_params,
                controller_config=controller_config,
                dt=self.step_dt,
                num_envs=self.num_envs,
                device=self.device,
            )

        # Pre-allocate a tensor for CTBR controller body rate setpoints
        self.body_rate_setpoints = torch.zeros(
            (self.num_envs, 3), dtype=torch.float, device=self.device
        )

        # Define action bounds for inverse normalization of actions
        if self.uses_ctbr_actions:
            # Construct thrust bounds tensor
            thrust_bounds = torch.tensor(
                [[0, EnvActionBounds.MAX_THRUST]],
                dtype=torch.float,
                device=self.device,
            )

            # Construct angular velocity bounds tensor
            max_ang_vels = torch.tensor(
                EnvActionBounds.MAX_ANG_VELS,
                dtype=torch.float,
                device=self.device,
            )

            # Remove yaw angular velocity bound
            # if the action type is CTBR_FIXED_YAW
            if self.action_type == EnvActionType.CTBR_FIXED_YAW:
                max_ang_vels = max_ang_vels[:-1]

            ang_vel_bounds = torch.hstack(
                [-max_ang_vels.view(-1, 1), max_ang_vels.view(-1, 1)]
            )

            # Construct CTBR action bounds tensor
            self.action_bounds = torch.vstack([thrust_bounds, ang_vel_bounds])
        else:
            # Construct rotor RPM action bounds tensor
            self.action_bounds = torch.tensor(
                [[EnvActionBounds.MIN_RPM, EnvActionBounds.MAX_RPM]],
                dtype=torch.float,
                device=self.device,
            )

        self.num_commands = command_cfg["num_commands"]

        self.simulate_action_latency = env_cfg["simulate_action_latency"]
        self.max_episode_length = math.ceil(
            env_cfg["episode_length_s"] / self.step_dt
        )

        self.env_cfg = env_cfg
        self.obs_cfg = obs_cfg
        self.reward_cfg = reward_cfg
        self.command_cfg: dict[str, tuple[float, float]] = command_cfg

        self.obs_scales = obs_cfg["obs_scales"]
        self.reward_scales = reward_cfg["reward_scales"]

        # Retrieve camera configuration
        if camera_config is None:
            camera_config = {}

        self.camera_res = camera_config.get("res", (640, 480))
        self.camera_pos = camera_config.get("pos", (3.0, 0.0, 3.0))
        self.camera_lookat = camera_config.get("lookat", (0.0, 0.0, 1.0))
        self.camera_fov = camera_config.get("fov", 40)

        # create scene
        self.scene = gs.Scene(
            sim_options=gs.options.SimOptions(dt=self.dt, substeps=1),
            viewer_options=gs.options.ViewerOptions(
                max_FPS=env_cfg["max_visualize_FPS"],
                camera_pos=self.camera_pos,
                camera_lookat=self.camera_lookat,
                camera_fov=self.camera_fov,
            ),
            vis_options=gs.options.VisOptions(
                rendered_envs_idx=list(range(self.num_envs))
            ),
            rigid_options=gs.options.RigidOptions(
                dt=self.dt,
                constraint_solver=gs.constraint_solver.Newton,
                enable_collision=True,
                enable_joint_limit=True,
            ),
            show_viewer=show_viewer,
        )

        # add plane
        self.scene.add_entity(gs.morphs.Plane())

        # add target
        if self.env_cfg["visualize_target"]:
            self.target: RigidEntity = self.scene.add_entity(
                morph=gs.morphs.Mesh(
                    file="meshes/sphere.obj",
                    scale=0.04,
                    fixed=True,
                    collision=False,
                ),
                surface=gs.surfaces.Rough(
                    diffuse_texture=gs.textures.ColorTexture(
                        color=(1.0, 0.5, 0.5),
                    ),
                ),
            )
        else:
            self.target = None

        # add camera
        if self.env_cfg["visualize_camera"]:
            self.cam = self.scene.add_camera(
                res=self.camera_res,
                pos=self.camera_pos,
                lookat=self.camera_lookat,
                fov=self.camera_fov,
                GUI=True,
            )

        # add drone
        self.base_init_pos = torch.tensor(
            self.env_cfg["base_init_pos"], device=self.device
        )
        self.base_init_quat = torch.tensor(
            self.env_cfg["base_init_quat"], device=self.device
        )
        self.inv_base_init_quat = inv_quat(self.base_init_quat)
        self.drone: DroneEntity = self.scene.add_entity(
            gs.morphs.Drone(file="urdf/drones/cf2x.urdf")
        )

        # build scene
        self.scene.build(n_envs=num_envs, env_spacing=(0.4, 0.4))

        # prepare reward functions and multiply reward scales by dt
        self.reward_functions, self.episode_sums = {}, {}
        for name in self.reward_scales.keys():
            self.reward_scales[name] *= self.step_dt
            self.reward_functions[name] = getattr(self, "_reward_" + name)
            self.episode_sums[name] = torch.zeros(
                (self.num_envs,), device=self.device, dtype=gs.tc_float
            )

        # initialize buffers
        self.obs_buf = torch.zeros(
            (self.num_envs, self.num_obs),
            device=self.device,
            dtype=gs.tc_float,
        )
        self.rew_buf = torch.zeros(
            (self.num_envs,), device=self.device, dtype=gs.tc_float
        )
        self.reset_buf = torch.ones(
            (self.num_envs,), device=self.device, dtype=gs.tc_int
        )
        self.episode_length_buf = torch.zeros(
            (self.num_envs,), device=self.device, dtype=gs.tc_int
        )
        self.commands = torch.zeros(
            (self.num_envs, self.num_commands),
            device=self.device,
            dtype=gs.tc_float,
        )

        self.actions = torch.zeros(
            (self.num_envs, self.num_actions),
            device=self.device,
            dtype=gs.tc_float,
        )
        self.last_actions = torch.zeros_like(self.actions)

        self.base_pos = torch.zeros(
            (self.num_envs, 3), device=self.device, dtype=gs.tc_float
        )
        self.base_quat = torch.zeros(
            (self.num_envs, 4), device=self.device, dtype=gs.tc_float
        )
        self.base_lin_vel = torch.zeros(
            (self.num_envs, 3), device=self.device, dtype=gs.tc_float
        )
        self.base_ang_vel = torch.zeros(
            (self.num_envs, 3), device=self.device, dtype=gs.tc_float
        )
        self.last_base_pos = torch.zeros_like(self.base_pos)

        # Configure actuator and observation noise
        self.actuator_noise_std: float = env_cfg["actuator_noise_std"]
        self.obs_noise_std: float = obs_cfg["obs_noise_std"]

        self.extras: dict[str, Any] = {}  # extra information for logging
        self.extras["observations"] = {}

        # Configure minimum hover time at target for target updates
        self.min_hover_time_s = env_cfg["min_hover_time_s"]
        self.min_hover_steps = math.ceil(self.min_hover_time_s / self.step_dt)
        self.at_target_threshold = env_cfg["at_target_threshold"]
        self.at_target_threshold_square = self.at_target_threshold**2

        # Initialize buffer to count steps at target
        self.hover_counter = torch.zeros(
            (self.num_envs,), device=self.device, dtype=torch.int
        )

        # Enable automatic target updates (commands)
        # If True, target positions are updated when reached.
        # If False, they must be manually changed.
        self.auto_target_updates = auto_target_updates

        # Enable drawing debug colored spheres over
        # drones if drone colors are provided
        self.drone_colors_enabled = False
        self.initialize_debug_color_spheres(drone_colors)

        # Flag for video recording
        # Enables camera rendering during simulation steps when True.
        # Automatically set by `start_recording()`
        self.render = False

    def _resample_commands(self, envs_idx: torch.Tensor) -> None:
        self.commands[envs_idx, 0] = gs_rand_float(
            *self.command_cfg["pos_x_range"], (len(envs_idx),), self.device
        )
        self.commands[envs_idx, 1] = gs_rand_float(
            *self.command_cfg["pos_y_range"], (len(envs_idx),), self.device
        )
        self.commands[envs_idx, 2] = gs_rand_float(
            *self.command_cfg["pos_z_range"], (len(envs_idx),), self.device
        )

        if self.target is not None:
            self.target.set_pos(
                self.commands[envs_idx], zero_velocity=True, envs_idx=envs_idx
            )

    def _hovering_at_target(self) -> torch.Tensor:
        # Get a mask identifying which drones (env indices) are at target
        at_target_mask = (
            torch.norm(self.rel_pos, dim=1) < self.at_target_threshold
        )

        if self.min_hover_steps > 0:
            # Increment counters of drones that are at target
            self.hover_counter[at_target_mask] += 1
            # Reset counters for drones that moved away from the target
            self.hover_counter[~at_target_mask] = 0

            # Get indices of drones stabilized at target
            # (hovered at target for at least `min_hover_steps` steps)
            stabilized_at_target = (
                (self.hover_counter >= self.min_hover_steps)
                .nonzero(as_tuple=False)
                .flatten()
            )

            return stabilized_at_target
        else:
            # Get indices of drones that reached the target
            at_target = at_target_mask.nonzero(as_tuple=False).flatten()

            return at_target

    def reset(self) -> tuple[torch.Tensor, None]:
        self.reset_buf[:] = True
        self.reset_idx(torch.arange(self.num_envs, device=self.device))
        return self.obs_buf, None

    def step(
        self, actions: torch.Tensor
    ) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor, dict[str, Any]]:
        self.actions = torch.clip(
            actions,
            -self.env_cfg["clip_actions"],
            self.env_cfg["clip_actions"],
        )

        # Simulate action latency
        exec_actions = (
            self.last_actions if self.simulate_action_latency else self.actions
        )

        # Compute rotor RPMs from actions
        if self.action_type in (
            EnvActionType.CTBR,
            EnvActionType.CTBR_FIXED_YAW,
        ):
            # Calculate thrust and rate setpoints from actions
            ctbr_action = linear_interpolate(
                x=exec_actions,
                x_min=-1,
                x_max=1,
                y_min=self.action_bounds[:, 0],
                y_max=self.action_bounds[:, 1],
            )

            if self.action_type == EnvActionType.CTBR:
                # Assign w_x, w_y, w_z body rates to rate setpoints
                self.body_rate_setpoints[:, :] = ctbr_action[:, 1:]
            else:
                # Only assign w_x, and w_y body rates to rate setpoints,
                # while leaving w_z as 0 from initialization
                self.body_rate_setpoints[:, :2] = ctbr_action[:, 1:]

            # Compute rotor RPMs from CTBR controller
            rotor_RPMs = self.ctbr_controller.compute(
                rate_measurements=self.base_ang_vel,
                rate_setpoints=self.body_rate_setpoints,
                thrust_setpoints=ctbr_action[:, 0],
            )

        else:
            # Calculate rotor RPMs directly from actions
            rotor_RPMs = linear_interpolate(
                x=exec_actions,
                x_min=-1,
                x_max=1,
                y_min=self.action_bounds[:, 0],
                y_max=self.action_bounds[:, 1],
            )

        # Add actuator noise to rotor RPMs
        if self.actuator_noise_std > 0.0:
            rotor_RPMs = self._add_noise(rotor_RPMs, self.actuator_noise_std)
            rotor_RPMs = rotor_RPMs.clamp(
                EnvActionBounds.MIN_RPM, EnvActionBounds.MAX_RPM
            )

        # perform physics stepping
        for _ in range(self.decimation):
            self.drone.set_propellels_rpm(rotor_RPMs)
            self.scene.step()

            # Draw debug spheres for visual differentiation if enabled
            if self.drone_colors_enabled:
                self.draw_colored_spheres()

            # Render camera view if recording
            if self.render:
                self.cam.render()

        # update buffers
        self.episode_length_buf += 1
        self.last_base_pos[:] = self.base_pos[:]
        self.base_pos[:] = self.drone.get_pos()
        self.rel_pos = self.commands - self.base_pos
        self.last_rel_pos = self.commands - self.last_base_pos
        self.base_quat[:] = self.drone.get_quat()
        self.base_euler = quat_to_xyz(
            transform_quat_by_quat(
                torch.ones_like(self.base_quat) * self.inv_base_init_quat,
                self.base_quat,
            ),
            rpy=True,
            degrees=True,
        )
        inv_base_quat = inv_quat(self.base_quat)
        self.base_lin_vel[:] = transform_by_quat(
            self.drone.get_vel(), inv_base_quat
        )
        self.base_ang_vel[:] = transform_by_quat(
            self.drone.get_ang(), inv_base_quat
        )

        # resample commands automatically if enabled
        if self.auto_target_updates:
            stabilized_envs_idx = self._hovering_at_target()

            if len(stabilized_envs_idx) > 0:
                self._resample_commands(stabilized_envs_idx)
                self.hover_counter[stabilized_envs_idx] = 0

        # check termination and reset
        self.crash_condition = (
            (
                torch.abs(self.base_euler[:, 1])
                > self.env_cfg["termination_if_pitch_greater_than"]
            )
            | (
                torch.abs(self.base_euler[:, 0])
                > self.env_cfg["termination_if_roll_greater_than"]
            )
            | (
                torch.abs(self.rel_pos[:, 0])
                > self.env_cfg["termination_if_x_greater_than"]
            )
            | (
                torch.abs(self.rel_pos[:, 1])
                > self.env_cfg["termination_if_y_greater_than"]
            )
            | (
                torch.abs(self.rel_pos[:, 2])
                > self.env_cfg["termination_if_z_greater_than"]
            )
            | (
                self.base_pos[:, 2]
                < self.env_cfg["termination_if_close_to_ground"]
            )
            | (
                torch.any(
                    torch.abs(self.base_ang_vel)
                    > self.env_cfg["termination_if_ang_vel_greater_than"],
                    dim=1,
                )
            )
            | (
                torch.any(
                    torch.abs(self.base_lin_vel)
                    > self.env_cfg["termination_if_lin_vel_greater_than"],
                    dim=1,
                )
            )
        )
        self.reset_buf = (
            self.episode_length_buf > self.max_episode_length
        ) | self.crash_condition

        time_out_idx = self.episode_length_buf > self.max_episode_length
        time_out_idx = time_out_idx.nonzero(as_tuple=False).flatten()
        self.extras["time_outs"] = torch.zeros_like(
            self.reset_buf, device=self.device, dtype=gs.tc_float
        )
        self.extras["time_outs"][time_out_idx] = 1.0

        self.reset_idx(self.reset_buf.nonzero(as_tuple=False).flatten())

        # compute reward
        self.rew_buf[:] = 0.0
        for name, reward_func in self.reward_functions.items():
            rew = reward_func() * self.reward_scales[name]
            self.rew_buf += rew
            self.episode_sums[name] += rew

        # compute observations
        self.obs_buf = self.compute_observations()

        self.last_actions[:] = self.actions[:]

        # Return obs, rewards, dones, infos
        return self.obs_buf, self.rew_buf, self.reset_buf, self.extras

    def close(self) -> None:
        if not self._is_closed:
            del self.scene

            # update closing status
            self._is_closed = True

    def reset_idx(self, envs_idx: torch.Tensor) -> None:
        if len(envs_idx) == 0:
            return

        # reset base
        self.base_pos[envs_idx] = self.base_init_pos
        self.last_base_pos[envs_idx] = self.base_init_pos
        self.rel_pos = self.commands - self.base_pos
        self.last_rel_pos = self.commands - self.last_base_pos
        self.base_quat[envs_idx] = self.base_init_quat.reshape(1, -1)
        self.drone.set_pos(
            self.base_pos[envs_idx], zero_velocity=True, envs_idx=envs_idx
        )
        self.drone.set_quat(
            self.base_quat[envs_idx], zero_velocity=True, envs_idx=envs_idx
        )
        self.base_lin_vel[envs_idx] = 0
        self.base_ang_vel[envs_idx] = 0
        self.drone.zero_all_dofs_velocity(envs_idx)

        # reset buffers
        self.last_actions[envs_idx] = 0.0
        self.episode_length_buf[envs_idx] = 0
        self.reset_buf[envs_idx] = True
        self.hover_counter[envs_idx] = 0

        # fill extras
        self.extras["episode"] = {}
        for key in self.episode_sums.keys():
            self.extras["episode"]["rew_" + key] = (
                torch.mean(self.episode_sums[key][envs_idx]).item()
                / self.env_cfg["episode_length_s"]
            )
            self.episode_sums[key][envs_idx] = 0.0

        # Reset CTBR controller state if a CTBR controller is used
        if self.uses_ctbr_actions:
            self.ctbr_controller.reset()

        self._resample_commands(envs_idx)

    def compute_observations(self) -> torch.Tensor:
        # Normalize observations to the [-1, 1] range
        pos = torch.clip(self.rel_pos * self.obs_scales["rel_pos"], -1, 1)
        quat = self.base_quat.clone()
        lin_vel = torch.clip(
            self.base_lin_vel * self.obs_scales["lin_vel"], -1, 1
        )
        ang_vel = torch.clip(
            self.base_ang_vel * self.obs_scales["ang_vel"], -1, 1
        )
        last_actions = self.last_actions

        # Add noise to observations except last actions
        if self.obs_noise_std > 0.0:
            pos = self._add_noise(pos, self.obs_noise_std)
            lin_vel = self._add_noise(lin_vel, self.obs_noise_std)
            ang_vel = self._add_noise(ang_vel, self.obs_noise_std)

            # Add noise to quaternions
            # Note:
            # Directly adding Gaussian noise to quaternions and then
            # normalizing them approximates valid rotation noise only
            # for small standard deviations.
            quat = self._add_noise(quat, self.obs_noise_std)
            quat = quat / quat.norm(dim=1, keepdim=True)

        obs_buf = torch.cat(
            [pos, quat, lin_vel, ang_vel, last_actions], dim=-1
        )

        return obs_buf

    def get_observations(self) -> tuple[torch.Tensor, dict[str, Any]]:
        return self.obs_buf, self.extras

    def get_pos(self, add_noise: bool = True) -> torch.Tensor:
        """Get the drone's position with optional noise."""
        if add_noise and self.obs_noise_std > 0.0:
            # Note:
            # Gaussian noise is added to normalized observations, so it
            # must be scaled when applied to absolute positions.
            pos_noise = (
                torch.randn_like(self.base_pos)
                * self.obs_noise_std
                / self.obs_scales["rel_pos"]
            )

            return self.base_pos + pos_noise

        return self.base_pos

    def get_quat(self, add_noise: bool = True) -> torch.Tensor:
        """Get the drone's quaternion with optional noise."""
        if add_noise and self.obs_noise_std > 0.0:
            # Note:
            # This Gaussian noise addition approximates valid rotation
            # noise only for small standard deviations
            quat = self._add_noise(self.base_quat, self.obs_noise_std)
            quat = quat / quat.norm(dim=1, keepdim=True)

            return quat

        return self.base_quat

    def _add_noise(
        self, input_tensor: torch.Tensor, noise_std: float
    ) -> torch.Tensor:
        return input_tensor + torch.randn_like(input_tensor) * noise_std

    # ------------ target position update ------------
    def update_target_pos(
        self, envs_idx: torch.Tensor, target_pos: torch.Tensor
    ) -> None:
        self.commands[envs_idx, :] = target_pos

        if self.target is not None:
            self.target.set_pos(
                self.commands[envs_idx],
                zero_velocity=True,
                envs_idx=envs_idx,
            )

    # ------------ save/load state ------------
    def get_current_state(self, envs_idx: torch.Tensor) -> EnvState:
        ctbr_state = None
        if self.uses_ctbr_actions:
            # Add CTBR controller state if a CTBR controller is used
            ctbr_state = self.ctbr_controller.get_state()

        return EnvState(
            base_pos=self.base_pos[envs_idx].clone(),
            base_quat=self.base_quat[envs_idx].clone(),
            base_lin_vel=self.base_lin_vel[envs_idx].clone(),
            base_ang_vel=self.base_ang_vel[envs_idx].clone(),
            commands=self.commands[envs_idx].clone(),
            episode_length=self.episode_length_buf[envs_idx].clone(),
            last_actions=self.last_actions[envs_idx].clone(),
            ctbr_controller_state=ctbr_state,
        )

    def restore_from_state(
        self, envs_idx: torch.Tensor, saved_state: EnvState
    ) -> None:
        # Retrieve and clone variables from saved state
        base_pos = saved_state.base_pos.clone()
        base_quat = saved_state.base_quat.clone()
        base_lin_vel = saved_state.base_lin_vel.clone()
        base_ang_vel = saved_state.base_ang_vel.clone()
        commands = saved_state.commands.clone()
        episode_length = saved_state.episode_length.clone()
        last_actions = saved_state.last_actions.clone()

        if self.uses_ctbr_actions:
            assert saved_state.ctbr_controller_state is not None
            ctbr_controller_state = saved_state.ctbr_controller_state.clone()

        # Reset base
        self.base_pos[envs_idx] = base_pos
        self.last_base_pos[envs_idx] = base_pos
        self.rel_pos[envs_idx] = commands - base_pos
        self.last_rel_pos[envs_idx] = commands - base_pos
        self.base_quat[envs_idx] = base_quat.reshape(1, -1)

        # Apply position and orientation
        self.drone.set_pos(
            self.base_pos[envs_idx], zero_velocity=False, envs_idx=envs_idx
        )
        self.drone.set_quat(
            self.base_quat[envs_idx], zero_velocity=False, envs_idx=envs_idx
        )

        # Apply linear and angular velocities
        self.base_lin_vel[envs_idx] = base_lin_vel
        self.base_ang_vel[envs_idx] = base_ang_vel

        qvel = torch.cat(
            [self.base_lin_vel[envs_idx], self.base_ang_vel[envs_idx]], dim=-1
        )
        self.drone.set_dofs_velocity(qvel, envs_idx=[envs_idx])

        # Reset buffers
        self.last_actions[envs_idx] = last_actions
        self.episode_length_buf[envs_idx] = episode_length
        self.reset_buf[envs_idx] = True

        # Fill extras
        self.extras["episode"] = {}
        for key in self.episode_sums.keys():
            self.extras["episode"]["rew_" + key] = (
                torch.mean(self.episode_sums[key][envs_idx]).item()
                / self.env_cfg["episode_length_s"]
            )
            self.episode_sums[key][envs_idx] = 0.0

        # Load CTBR controller state if the action type is CTBR
        if self.uses_ctbr_actions:
            self.ctbr_controller.load_state(ctbr_controller_state)

    # ------------ debug colored spheres drawing ----------------
    def initialize_debug_color_spheres(
        self, drone_colors: list[tuple[float, float, float, float]] | None
    ) -> None:
        # Enable drawing debug colored spheres over
        # drones if drone colors are provided
        if drone_colors:
            if len(drone_colors) != self.num_envs:
                raise ValueError(
                    "The number of colors in `drone_colors` must be equal to "
                    "the number of environments."
                )

            self.drone_colors = drone_colors
            self.drone_offsets = torch.from_numpy(self.scene.envs_offset).to(
                device=self.device, dtype=torch.float
            )
            self.drone_colors_enabled = True

    def draw_colored_spheres(self) -> None:
        # Draw color debug spheres over drones for
        # visual differentiation if enabled
        self.scene.clear_debug_objects()

        # Calculate drone world positions
        local_pos = self.drone.get_pos()
        world_pos = local_pos + self.drone_offsets

        # Draw colored spheres for each drone
        for i in range(self.num_envs):
            self.scene.draw_debug_sphere(
                pos=world_pos[i],
                radius=0.03,
                color=self.drone_colors[i],
            )

    # ------------ video recording functions----------------
    def start_recording(self) -> None:
        """Start video recording using the environment's camera."""
        if hasattr(self, "cam"):
            self.cam.start_recording()
            self.render = True
        else:
            raise RuntimeError(
                "Cannot start recording: camera is not initialized."
                "Set `visualize_camera` to True in `env_cfg`."
            )

    def stop_and_save_recording(
        self, save_to_filename: str = "output.mp4", fps: int = 60
    ) -> None:
        """Stop video recording and save the video to a file."""
        if hasattr(self, "cam"):
            self.cam.stop_recording(save_to_filename=save_to_filename, fps=fps)
        else:
            raise RuntimeError(
                "Cannot stop recording: camera is not initialized."
                "Set `visualize_camera` to True in `env_cfg`."
            )

    # ------------ reward functions----------------
    def _reward_target(self) -> torch.Tensor:
        target_rew = torch.sum(
            torch.square(self.last_rel_pos), dim=1
        ) - torch.sum(torch.square(self.rel_pos), dim=1)
        return target_rew

    def _reward_closeness(self) -> torch.Tensor:
        dist_square = torch.sum(torch.square(self.rel_pos), dim=1)

        # Reward is 1 at the target and decreases linearly to 0
        # at `at_target_threshold` distance
        closeness_rew = torch.clamp(
            (self.at_target_threshold_square - dist_square)
            / self.at_target_threshold_square,
            min=0.0,
        )

        return closeness_rew

    def _reward_hover_time(self) -> torch.Tensor:
        hover_rew = self.hover_counter
        return hover_rew

    def _reward_smooth(self) -> torch.Tensor:
        smooth_rew = torch.sum(
            torch.square(self.actions - self.last_actions), dim=1
        )
        return smooth_rew

    def _reward_yaw(self) -> torch.Tensor:
        yaw = self.base_euler[:, 2]
        yaw = torch.where(yaw > 180, yaw - 360, yaw) / 180 * 3.14159  # (rad)
        yaw_rew = torch.exp(self.reward_cfg["yaw_lambda"] * torch.abs(yaw))
        return yaw_rew

    def _reward_angular(self) -> torch.Tensor:
        angular_rew = torch.norm(self.base_ang_vel / 3.14159, dim=1)
        return angular_rew

    def _reward_crash(self) -> torch.Tensor:
        crash_rew = torch.zeros(
            (self.num_envs,), device=self.device, dtype=gs.tc_float
        )
        crash_rew[self.crash_condition] = 1
        return crash_rew

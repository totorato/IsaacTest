import math
import numpy as np
import os
import torch
import xml.etree.ElementTree as ET

from isaacgym import gymutil, gymtorch, gymapi
from isaacgym.torch_utils import *
from tasks.base.vec_task import VecTask

def _indent_xml(elem, level=0):
	i = "\n" + level * "  "
	if len(elem):
		if not elem.text or not elem.text.strip():
			elem.text = i + "  "
		if not elem.tail or not elem.tail.strip():
			elem.tail = i
		for elem in elem:
			_indent_xml(elem, level + 1)
		if not elem.tail or not elem.tail.strip():
			elem.tail = i
	else:
		if level and (not elem.tail or not elem.tail.strip()):
			elem.tail = i

class BallBalance(VecTask) :

	def __init__(self, cfg, sim_device, graphics_device_id, headless) :
		
		""" cfg: config dict							"""
		""" sim_divice: where to simulate physics		""" # eg. 'cuda:0' or 'cpu'
		""" graphics_device_id: where to render			""" # eg. 1 means cuda:1
		""" headless: run without window				"""

		self.cfg = cfg
		self.max_episode_length = self.cfg["env"]["maxEpisodeLength"]
		self.action_speed_scale = self.cfg["env"]["actionSpeedScale"]
		self.debug_viz = self.cfg["env"]["enableDebugVis"]

		sensors_per_env = 3
		actors_per_env = 2
		dofs_per_env = 6
		bodies_per_env = 7+1	# 7 parts in bot, 1 part ball

		#################################################
		# Observations:									#
		# 0:3 - activated DOF positions					#
		# 3:6 - activated DOF velocities				#
		# 6:9 -	ball position							#
		# 9:12 - ball velocity							#
		# 12:15 - sensor force							#
		# 15:24 - sensor torques						#
		#################################################

		self.cfg["env"]["numObservations"] = 24
		self.cfg["env"]["numActions"] = 3

		super().__init__(config=self.cfg, sim_device=sim_device, graphics_device_id=graphics_device_id, headless=headless)
		self.root_tensor = self.gym.acquire_actor_root_state_tensor(self.sim)
		self.dof_state_tensor = self.gym.acquire_dof_state_tensor(self.sim)
		self.sensor_tensor = self.gym.acquire_force_sensor_tensor(self.sim)

		print("root tensor:", self.root_tensor.shape)
		
		vec_root_tensor = gymtorch.wrap_tensor(self.root_tensor).view(self.num_envs, dofs_per_env, 13)
		vec_dof_tensor = gymtorch.wrap_tensor(self.root_tensor).view(self.num_envs, dofs_per_env, 2)
		vec_sensor_tensor = gymtorch.wrap_tensor(self.sensor_tensor).view(self.num_envs, sensors_per_env, 6)

		self.root_states = vec_root_tensor
		self.tray_positions = vec_root_tensor[..., 0, 0:3]
		self.ball_positions = vec_root_tensor[..., 1, 0:3]
		self.ball_orientations = vec_root_tensor[..., 1, 3:7]
		self.ball_linvels = vec_root_tensor[..., 1, 7:10]	# linear velocity
		self.ball_angvels = vec_root_tensor[..., 1, 10:13]	# angular velocity

		self.dof_states = vec_dof_tensor
		self.dof_positions = vec_dof_tensor[..., 0]
		self.dof_velocities = vec_dof_tensor[..., 1]

		self.sensor_forces = vec_sensor_tensor[..., 0:3]
		self.sensor_torques = vec_sensor_tensor[..., 3:6]

		self.gym.refresh_actor_root_state_tensor(self.sim)
		self.gym.refresh_dof_state_tensor(self.sim)

		self.initial_dof_states = self.dof_states.clone()
		self.initial_root_states = self.root_states.clone()

		self.dof_position_targets = torch.zeros((self.num_envs, dofs_per_env), dtype=torch.float32, device=self.device, requires_grad=False)

		self.all_actor_indices = torch.arange(actors_per_env * self.num_envs, dtype=torch.int32, device=self.device).view(self.num_envs, actors_per_env)
		self.all_bot_indices = actors_per_env * torch.arange(self.num_envs, dtype=torch.int32, device=self.device)

		self.axes_geom = gymutil.AxesGeometry(0.2)
	
	def create_sim(self) :
		
		self.dt = self.sim_params.dt
		self.sim_params.up_axis = gymapi.UP_AXIS_Z
		self.sim_params.gravity.x = 0
		self.sim_params.gravity.y = 0
		self.sim_params.gravity.z = -9.81

		self.sim = super().create_sim(self.device_id, self.graphics_device_id, self.physics_engine, self.sim_params)

		# TODO
		self._create_balance_bot_file()	# create a xml file

	def _create_balance_bot_file(self) :

		tray_radius = 0.5
		tray_thickness = 0.02
		leg_radius = 0.02
		leg_outer_offset = tray_radius - 0.1
		leg_length = leg_outer_offset - 2 * leg_radius
		leg_inner_offset = leg_outer_offset - leg_length / math.sqrt(2)

		tray_height = leg_length * math.sqrt(2) + 2 * leg_radius + 0.5 * tray_thickness

		root = ET.Element('mujoco')
		root.attrib["model"] = "BalanceBot"
		compiler = ET.SubElement(root, "compiler")
		compiler.attrib["angle"] = "degree"
		compiler.attrib["coordinate"] = "local"
		compiler.attrib["inertiafromgeom"] = "true"
		worldbody = ET.SubElement(root, "worldbody")

		tray = ET.SubElement(worldbody, "body")
		tray.attrib["name"] = "tray"
		tray.attrib["pos"] = "%g %g %g" % (0, 0, tray_height)
		tray_joint = ET.SubElement(tray, "joint")
		tray_joint.attrib["name"] = "root_joint"
		tray_joint.attrib["type"] = "free"
		tray_geom = ET.SubElement(tray, "geom")
		tray_geom.attrib["type"] = "cylinder"
		tray_geom.attrib["size"] = "%g %g" % (tray_radius, 0.5 * tray_thickness)
		tray_geom.attrib["pos"] = "0 0 0"
		tray_geom.attrib["density"] = "100"

		leg_angles = [0.0, 2.0 / 3.0 * math.pi, 4.0 / 3.0 * math.pi]
		for i in range(len(leg_angles)):
			angle = leg_angles[i]

			upper_leg_from = gymapi.Vec3()
			upper_leg_from.x = leg_outer_offset * math.cos(angle)
			upper_leg_from.y = leg_outer_offset * math.sin(angle)
			upper_leg_from.z = -leg_radius - 0.5 * tray_thickness
			upper_leg_to = gymapi.Vec3()
			upper_leg_to.x = leg_inner_offset * math.cos(angle)
			upper_leg_to.y = leg_inner_offset * math.sin(angle)
			upper_leg_to.z = upper_leg_from.z - leg_length / math.sqrt(2)
			upper_leg_pos = (upper_leg_from + upper_leg_to) * 0.5
			upper_leg_quat = gymapi.Quat.from_euler_zyx(0, -0.75 * math.pi, angle)
			upper_leg = ET.SubElement(tray, "body")
			upper_leg.attrib["name"] = "upper_leg" + str(i)
			upper_leg.attrib["pos"] = "%g %g %g" % (upper_leg_pos.x, upper_leg_pos.y, upper_leg_pos.z)
			upper_leg.attrib["quat"] = "%g %g %g %g" % (upper_leg_quat.w, upper_leg_quat.x, upper_leg_quat.y, upper_leg_quat.z)
			upper_leg_geom = ET.SubElement(upper_leg, "geom")
			upper_leg_geom.attrib["type"] = "capsule"
			upper_leg_geom.attrib["size"] = "%g %g" % (leg_radius, 0.5 * leg_length)
			upper_leg_geom.attrib["density"] = "1000"
			upper_leg_joint = ET.SubElement(upper_leg, "joint")
			upper_leg_joint.attrib["name"] = "upper_leg_joint" + str(i)
			upper_leg_joint.attrib["type"] = "hinge"
			upper_leg_joint.attrib["pos"] = "%g %g %g" % (0, 0, -0.5 * leg_length)
			upper_leg_joint.attrib["axis"] = "0 1 0"
			upper_leg_joint.attrib["limited"] = "true"
			upper_leg_joint.attrib["range"] = "-45 45"

			lower_leg_pos = gymapi.Vec3(-0.5 * leg_length, 0, 0.5 * leg_length)
			lower_leg_quat = gymapi.Quat.from_euler_zyx(0, -0.5 * math.pi, 0)
			lower_leg = ET.SubElement(upper_leg, "body")
			lower_leg.attrib["name"] = "lower_leg" + str(i)
			lower_leg.attrib["pos"] = "%g %g %g" % (lower_leg_pos.x, lower_leg_pos.y, lower_leg_pos.z)
			lower_leg.attrib["quat"] = "%g %g %g %g" % (lower_leg_quat.w, lower_leg_quat.x, lower_leg_quat.y, lower_leg_quat.z)
			lower_leg_geom = ET.SubElement(lower_leg, "geom")
			lower_leg_geom.attrib["type"] = "capsule"
			lower_leg_geom.attrib["size"] = "%g %g" % (leg_radius, 0.5 * leg_length)
			lower_leg_geom.attrib["density"] = "1000"
			lower_leg_joint = ET.SubElement(lower_leg, "joint")
			lower_leg_joint.attrib["name"] = "lower_leg_joint" + str(i)
			lower_leg_joint.attrib["type"] = "hinge"
			lower_leg_joint.attrib["pos"] = "%g %g %g" % (0, 0, -0.5 * leg_length)
			lower_leg_joint.attrib["axis"] = "0 1 0"
			lower_leg_joint.attrib["limited"] = "true"
			lower_leg_joint.attrib["range"] = "-70 90"

		_indent_xml(root)
		ET.ElementTree(root).write("balance_bot.xml")	# save the xml to file

		# save some useful robot parameters
		self.tray_height = tray_height
		self.leg_radius = leg_radius
		self.leg_length = leg_length
		self.leg_outer_offset = leg_outer_offset
		self.leg_angles = leg_angles
	
	def _create_ground_plane(self) :

		plane_params = gymapi.PlaneParams()
		plane_params.normal = gymapi.Vec3(0.0, 0.0, 1.0)
		self.gym.add_ground(self.sim, plane_params)
	
	def _create_envs(self, num_envs, spacing, num_per_row) :

		lower = gymapi.Vec3(-spacing, -spacing, 0.0)
		upper = gymapi.Vec3(spacing, spacing, spacing)

		asset_root = "."
		asset_file = "balance_bot.xml"

		asset_path  = os.path.join(asset_root, asset_file)
		asset_root = os.path.dirname(asset_path)
		asset_file = os.path.basename(asset_path)

		#####################################################################################
		
		""" asset configuration for bot begin """

		# load balance_bot.mxl
		bot_options = gymapi.AssetOptions()
		bot_options.fix_base_link = False
		bot_options.slices_per_cylinder = 40
		bot_asset = self.gym.load_asset(self.sim, asset_root, asset_file, bot_options)

		# no idea what is going on
		self.num_bot_dofs = self.gym.get_asset_dof_count(bot_asset)
		bot_dof_props = self.gym.get_asset_dof_properties(bot_asset)
		self.bot_dof_lower_limits = []
		self.bot_dof_upper_limits = []
		for i in range(self.num_bot_dofs) :
			self.bot_dof_lower_limits.append(bot_dof_props['lower'][i])
			self.bot_dof_upper_limits.append(bot_dof_props['upper'][i])

		self.bot_dof_lower_limits = to_torch(self.bot_dof_lower_limits, device=self.device)
		self.bot_dof_upper_limits = to_torch(self.bot_dof_upper_limits, device=self.device)

		# setting bot pose
		bot_pose = gymapi.Transform()
		bot_pose.p.z = self.tray_height

		# create force sensors on tray
		bot_tray_idx = self.gym.find_asset_rigid_body_index(bot_asset, "tray")
		for angle in self.leg_angles :
			sensor_pose = gymapi.Transform()
			sensor_pose.p.x = self.leg_outer_offset * math.cos(angle)
			sensor_pose.p.y = self.leg_outer_offset * math.sin(angle)
			self.gym.create_asset_force_sensor(bot_asset, bot_tray_idx, sensor_pose)

		""" asset configuration for bot end """

		#####################################################################################

		""" asset configuration for ball begin """

		# create ball asset
		self.ball_radius = 0.1
		ball_options = gymapi.AssetOptions()
		ball_options.density = 200
		ball_asset = self.gym.create_sphere(self.sim, self.ball_radius, ball_options)

		# setting ball pose
		ball_pose = gymapi.Transform()
		ball_pose.p.x = 0.2
		ball_pose.p.z = 2.0

		""" asset configuration for ball end """

		#####################################################################################

		""" asset placing into simulator """

		# place assets
		self.envs = []
		self.bot_handles = []
		self.obj_handles = []
		for i in range(self.num_envs) :
			# get the pointer to env
			env_ptr = self.gym.create_env(self.sim, lower, upper, num_per_row)
			self.envs.append(env_ptr)

			# place bot!!!
			bot_handle = self.gym.create_actor(env_ptr, bot_asset, bot_pose, "bot", i, 0, 0)
			self.bot_handles.append(bot_handle)

			# place ball!!!
			ball_handle = self.gym.create_actor(env_ptr, ball_asset, ball_pose, "ball", i, 0, 0)
			self.obj_handles.append(ball_handle)

			actuated_dofs = np.array([1,3,5])
			free_dofs = np.array([0, 2, 4])

			# set bot properties
			dof_props = self.gym.get_actor_dof_properties(env_ptr, bot_handle)
			dof_props['driveMode'][actuated_dofs] = gymapi.DOF_MODE_POS
			dof_props['stiffness'][actuated_dofs] = 4000.0
			dof_props['damping'][actuated_dofs] = 100.0
			dof_props['driveMode'][free_dofs] = gymapi.DOF_MODE_NONE
			dof_props['stiffness'][free_dofs] = 0
			dof_props['damping'][free_dofs] = 0
			self.gym.set_actor_dof_properties(env_ptr, bot_handle, dof_props)

			# find handles for legs. handles are used for fixing this bot
			lower_leg_handles = []
			lower_leg_handles.append(self.gym.find_actor_rigid_body_handle(env_ptr, bot_handle, "lower_leg0"))
			lower_leg_handles.append(self.gym.find_actor_rigid_body_handle(env_ptr, bot_handle, "lower_leg1"))
			lower_leg_handles.append(self.gym.find_actor_rigid_body_handle(env_ptr, bot_handle, "lower_leg2"))

			# create attractors to hold legs in place
			attractor_props = gymapi.AttractorProperties()
			attractor_props.stiffness = 5e7
			attractor_props.damping = 5e3
			attractor_props.axes = gymapi.AXIS_TRANSLATION
			for current_handle in lower_leg_handles :
				attractor_props.rigid_handle = current_handle
				attractor_props.target.p.x = self.leg_outer_offset * math.cos(angle)
				attractor_props.target.p.y = self.leg_outer_offset * math.sin(angle)
				attractor_props.target.p.z = self.leg_radius
				attractor_props.offset.p.z = 0.5 * self.leg_length
				# place attractor!!!
				self.gym.create_rigid_body_attractor(env_ptr, attractor_props)

			# fancy colors
			self.gym.set_rigid_body_color(env_ptr, ball_handle, 0, gymapi.MESH_VISUAL, gymapi.Vec3(0.99, 0.66, 0.25))
			self.gym.set_rigid_body_color(env_ptr, bot_handle, 0, gymapi.MESH_VISUAL, gymapi.Vec3(0.48, 0.65, 0.8))
			for j in range(1, 7) :
				self.gym.set_rigid_body_color(env_ptr, bot_handle, j, gymapi.MESH_VISUAL, gymapi.Vec3(0.15, 0.2, 0.3))

	def compute_observations(self) :

		actuated_dof_indices = torch.tensor([1,3,5], device=self.device)
		self.obs_buf[..., 0:3] = self.dof_positions[..., actuated_dof_indices]
		self.obs_buf[..., 3:6] = self.dof_velocities[..., actuated_dof_indices]
		self.obs_buf[..., 6:9] = self.ball_positions
		self.obs_buf[..., 9:12] = self.ball_linvels
		self.obs_buf[..., 12:15] = self.sensor_forces[..., 0]		# !!! need to add normalization !!!
		self.obs_buf[..., 15:18] = self.sensor_torques[..., 0]
		self.obs_buf[..., 18:21] = self.sensor_torques[..., 1]
		self.obs_buf[..., 21:24] = self.sensor_torques[..., 2]

		return self.obs_buf

	def compute_reward(self) :
		self.rew_buf[:], self.reset_buf[:] = compute_bot_reward(
			tray_positions=self.tray_positions,
			ball_positions=self.ball_positions,
			ball_velocities=self.ball_linvels,
			ball_radius=self.ball_radius,
			reset_buf=self.reset_buf,
			progress_buf=self.progress_buf,
			max_episode_length=self.max_episode_length
		)
		pass

	def reset_idx(self, env_ids) :
		
		num_resets = len(env_ids)

		self.root_states[env_ids] = self.initial_root_states[env_ids]

		min_d = 0.001
		max_d = 0.5
		min_height = 1.0
		max_height = 2.0
		min_speed_xy = 0
		max_speed_xy = 0

		dists = torch_rand_float(min_d, max_d, (num_resets, 1), self.device)
		dirs = torch_random_dir_2((num_resets, 1), self.device)
		hpos = dists * dirs
		vpos = torch_rand_float(min_height, max_height, (num_resets, 1), self.device)

		speedscales = (dists - min_d) / (max_d - min_d)
		hspeeds = torch_rand_float(min_speed_xy, max_speed_xy, (num_resets, 1), self.device)
		hvels = - speedscales * hspeeds * dirs
		vspeeds = -torch_rand_float(5.0, 5.0, (num_resets, 1), self.device).squeeze()

		self.ball_positions[env_ids, 0] = hpos[..., 0]
		self.ball_positions[env_ids, 1] = hpos[..., 1]
		self.ball_positions[env_ids, 2] = vpos[...]
		self.ball_orientations[env_ids, 0:3] = 0	# quad
		self.ball_orientations[env_ids, 3] = 1
		self.ball_linvels[env_ids, 0] = hvels[..., 0]
		self.ball_linvels[env_ids, 1] = hvels[..., 1]
		self.ball_linvels[env_ids, 2] = vspeeds
		self.ball_angvels[env_ids] = 0	# ball initially have no angular momentum

		# reset root state for bots and balls
		actor_indices = self.all_actor_indices[env_ids].flatten()
		self.gym.set_actor_root_state_tensor_indexed(
			self.sim,
			self.root_tensor,
			gymtorch.unwrap_tensor(actor_indices),
			len(actor_indices)
		)

		# reset DOF states for bots
		bot_indices = self.all_bot_indices[env_ids].flatten()
		self.gym.set_dof_state_tensor_indexed(
			self.sim,
			self.dof_state_tensor,
			gymtorch.unwrap_tensor(bot_indices),
			len(bot_indices)
		)

		self.reset_buf[env_ids] = 0
		self.progress_buf[env_ids] = 0
	
	def pre_physics_step(self, _actions) :
		
		reset_env_ids = self.reset_buf.nonzero(as_tuple=False).squeeze(-1)
		if len(reset_env_ids) > 0 :
			self.reset_idx(reset_env_ids)
		
		actions = _actions.to(self.device)
		actuated_indices = torch.LongTensor([1,3,5])

		self.dof_position_targets[..., actuated_indices] += self.dt * self.action_speed_scale * actions
		self.dof_position_targets[:] = tensor_clamp(self.dof_position_targets, self.bot_dof_lower_limits, self.bot_dof_upper_limits)
		self.dof_position_targets[reset_env_ids] = 0
		self.gym.set_dof_position_target_tensor(self.sim, gymtorch.unwrap_tensor(self.dof_position_targets))
	
	def post_physics_step(self) :

		self.progress_buf += 1
		self.gym.refresh_actor_root_state_tensor(self.sim)
		self.gym.refresh_dof_state_tensor(self.sim)
		self.gym.refresh_force_sensor_tensor(self.sim)

		self.compute_observations()
		self.compute_reward()

		# visualize
		if self.viewer and self.debug_viz :
			self.gym.clear_lines(self.viewer)
			for i in range(self.num_envs) :
				env = self.envs[i]
				bot_handle = self.bot_handles[i]
				body_handles = []
				body_handles.append(self.gym.find_actor_rigid_body_handle(env, bot_handle, "upper_leg0"))
				body_handles.append(self.gym.find_actor_rigid_body_handle(env, bot_handle, "upper_leg1"))
				body_handles.append(self.gym.find_actor_rigid_body_handle(env, bot_handle, "upper_leg2"))
			for cur_handle in body_handles :
				lpose = self.gym.get_rigid_transform(env, cur_handle)
				gymutil.draw_lines(self.axes_geom, self.gym, self.viewer, env, lpose)

@torch.jit.script
def compute_bot_reward(tray_positions, ball_positions, ball_velocities, ball_radius, reset_buf, progress_buf, max_episode_length):
	# type: (Tensor, Tensor, Tensor, float, Tensor, Tensor, float) -> Tuple[Tensor, Tensor]
	# calculating the norm for ball distance to desired height above the ground plane (i.e. 0.7)
	ball_dist = torch.sqrt(ball_positions[..., 0] * ball_positions[..., 0] +
						   (ball_positions[..., 2] - 0.7) * (ball_positions[..., 2] - 0.7) +
						   (ball_positions[..., 1]) * ball_positions[..., 1])
	ball_speed = torch.sqrt(ball_velocities[..., 0] * ball_velocities[..., 0] +
							ball_velocities[..., 1] * ball_velocities[..., 1] +
							ball_velocities[..., 2] * ball_velocities[..., 2])
	pos_reward = 1.0 / (1.0 + ball_dist)
	speed_reward = 1.0 / (1.0 + ball_speed)
	reward = pos_reward * speed_reward

	# update the stopped sequences
	reset = torch.where(progress_buf >= max_episode_length - 1, torch.ones_like(reset_buf), reset_buf)
	reset = torch.where(ball_positions[..., 2] < ball_radius * 1.5, torch.ones_like(reset_buf), reset)

	return reward, reset
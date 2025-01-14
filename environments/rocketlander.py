import math

from Box2D.b2 import (edgeShape, circleShape, fixtureDef, polygonShape, revoluteJointDef, contactListener)
import Box2D
from gym.envs.classic_control import rendering
import gym
from gym import spaces
from gym.utils import seeding
import logging
import pyglet
from itertools import chain
from constants import *
import numpy as np

from constants import BARGE_LENGTH_X1_RATIO, BARGE_LENGTH_X2_RATIO


class ContactDetector(contactListener):
    def __init__(self, env):
        contactListener.__init__(self)
        self.env = env

    def BeginContact(self, contact):
        if self.env.left_barge == contact.fixtureA.body or self.env.left_barge == contact.fixtureB.body:
            for i in range(2):
                if self.env.legs[i] in [contact.fixtureA.body, contact.fixtureB.body]:
                    self.env.legs[i].ground_contact = True
            game_over = True
            for leg in self.env.legs:
                game_over = game_over and leg.ground_contact
            self.env.game_over = game_over and abs(self.env.lander.linearVelocity.x) < 1 and abs(
                self.env.lander.linearVelocity.y) < 1

    def EndContact(self, contact):
        if self.env.left_barge == contact.fixtureA.body or self.env.left_barge == contact.fixtureB.body:
            for i in range(2):
                if self.env.legs[i] in [contact.fixtureA.body, contact.fixtureB.body]:
                    self.env.legs[i].ground_contact = False


right_const_barge_coordinates = (2, 0.1, 35, 42)
right_const_barge_coordinates_edges = [
    (right_const_barge_coordinates[2], 0.1),
    (right_const_barge_coordinates[3], 0.1),
    (right_const_barge_coordinates[3], right_const_barge_coordinates[0]),
    (right_const_barge_coordinates[2], right_const_barge_coordinates[0])
]
left_const_barge_coordinates = (2, 0.1, 1, 8)
left_const_barge_coordinates_edges = [
    (left_const_barge_coordinates[2], 0.1),
    (left_const_barge_coordinates[3], 0.1),
    (left_const_barge_coordinates[3], left_const_barge_coordinates[0]),
    (left_const_barge_coordinates[2], left_const_barge_coordinates[0])
]

top_const_barge, bottom_const_barge, left_const_barge, right_const_barge = right_const_barge_coordinates
rocket_x, rocket_y = (right_const_barge + left_const_barge) / 2, top_const_barge + 1
rocket_initial_coordinates = (rocket_x, rocket_y)

targetX = (left_const_barge_coordinates[3] + left_const_barge_coordinates[2]) / 2


class RocketLander(gym.Env):
    metadata = {
        'render.modes': ['human', 'rgb_array'],
        'video.frames_per_second': FPS
    }

    def __init__(self, settings):
        self._seed()
        self.viewer = rendering.Viewer(VIEWPORT_W, VIEWPORT_H)
        self.viewer.set_bounds(0, W, 0, H)
        self.world = Box2D.b2World()

        self.main_base = None
        self.CONTACT_FLAG = False

        self.landing_coordinates = (4.5, 2)

        self.lander = None
        self.particles = []
        self.state = []
        self.prev_shaping = None

        self.observation_space = spaces.Box(np.array([-2, -1, -np.inf, -np.inf, -3.14, -1, 0, 0]), np.array([2, 3, np.inf, np.inf, 3.14, 1, 1, 1]), dtype=np.float32) # (x - distance, y -distance, velX, velY, angel, angelVel, leftIsLanded, rightIsLanded)
        self.lander_tilt_angle_limit = THETA_LIMIT

        self.game_over = False

        self.settings = settings
        self.dynamicLabels = {}
        self.staticLabels = {}

        self.impulsePos = (0, 0)

        #self.action_space = spaces.Box(np.array([[0], [0, -1], [-0.261]]), np.array([[1], [1, 0], [0.261]]), dtype=np.float32)  # Main Engine, Nozzle Angle, Left/Right Engine
        self.action_space = spaces.Box(np.array([0, -1, -0.261]), np.array([1, 1, 0.261]), dtype=np.float32)  # Main Engine, Nozzle Angle, Left/Right Engine
        self.untransformed_state = [0] * 6  # Non-normalized state

        self.steps_limit = 10000
        assert BARGE_LENGTH_X1_RATIO < BARGE_LENGTH_X2_RATIO, 'Barge Length X1 must be 0-1 and smaller than X2'

        self.landing_barge_coordinates = left_const_barge_coordinates_edges
        self._create_barges()
        self._reset()

    """ INHERITED """

    def _seed(self, seed=None):
        self.np_random, returned_seed = seeding.np_random(seed)
        return returned_seed

    def _reset(self):
        self.steps_limit = 10000
        self._destroy()

        self.game_over = False

        self.world.contactListener_bug_workaround = ContactDetector(self)
        self.world.contactListener = self.world.contactListener_bug_workaround

        self.helipad_y = left_const_barge_coordinates[0]
        self.bargeHeight = left_const_barge_coordinates[0]

        self.initial_mass = 0
        self.remaining_fuel = 0
        self.prev_shaping = 0
        self.CONTACT_FLAG = False

        # Engine Stats
        self.action_history = []

        # --- ROCKET ---
        self._create_rocket(rocket_initial_coordinates)
        # --- END ROCKET ---

        return self._step(np.array([0, 0, 0]))[
            0]  # Step through one action = [0, 0, 0] and return the state, reward etc.

    def reset(self):
        return self._reset()

    def _destroy(self):
        if not self.main_base: return
        self.world.contactListener = None
        self._clean_particles(True)
        self.main_base = None
        if self.lander:
            self.world.DestroyBody(self.lander)
            self.world.DestroyBody(self.legs[0])
            self.world.DestroyBody(self.legs[1])

    def _step(self, action):
        assert len(action) == 3  # Fe, Fs, psi
        info = {}

        # Shutdown all Engines upon contact with the ground
        if self.CONTACT_FLAG:
            action = [0, 0, 0]

        if self.settings.get('Vectorized Nozzle'):
            part = self.nozzle
            part.angle = self.lander.angle + float(action[2])  # This works better than motorSpeed
            if part.angle > NOZZLE_ANGLE_LIMIT:
                part.angle = NOZZLE_ANGLE_LIMIT
            elif part.angle < -NOZZLE_ANGLE_LIMIT:
                part.angle = -NOZZLE_ANGLE_LIMIT
        else:
            part = self.lander

        if self.lander.angle > math.pi:
            self.lander.angle -= math.pi * 2
        if self.lander.angle < -math.pi:
            self.lander.angle += math.pi * 2

        m_power = self.__main_engines_force_computation(action, rocketPart=part)
        s_power, engine_dir = self.__side_engines_force_computation(action)

        if self.settings.get('Gather Stats'):
            self.action_history.append([m_power, s_power * engine_dir, part.angle])

        # Decrease the rocket ass
        self._decrease_mass(m_power, s_power)

        # State Vector
        self.previous_state = self.state  # Keep a record of the previous state
        state, self.untransformed_state = self.__generate_state()  # Generate state
        self.state = state  # Keep a record of the new state

        # Rewards for reinforcement learning
        reward = self.__compute_rewards(state, m_power, s_power,
                                        part.angle)  # part angle can be used as part of the reward

        # Check if the game is done, adjust reward based on the final state of the body
        state_reset_conditions = [
            abs(state[XX]) >= 2.0,  # Rocket moves out of x-space
            state[YY] < -1 or state[YY] > 3,  # Rocket moves out of y-space or below barge
            # abs(state[THETA]) > THETA_LIMIT # Rocket tilts greater than the "controllable" limit
        ]
        done = False
        if any(state_reset_conditions):
            done = True
            reward = -10
            print('Conditions', state_reset_conditions, state[YY], state[XX])
        if not self.lander.awake:
            done = True
            reward = -1000
            print('Lander.awake')
        if self.game_over:
            done = True
            reward = 1000
            print('Game over')
        info['success'] = self.game_over
        if self.steps_limit == 0:
            done = True
            reward = -10
            print('Steps limit')
            self.steps_limit = 10000

        self.steps_limit -= 1
        self._update_particles()

        return np.array(state), reward, done, info

    def step(self, action):
        return self._step(action)

    def __main_engines_force_computation(self, action, rocketPart, *args):
        # ----------------------------------------------------------------------------
        # Nozzle Angle Adjustment

        # For readability
        sin = math.sin(rocketPart.angle)
        cos = math.cos(rocketPart.angle)

        # Random dispersion for the particles
        dispersion = [self.np_random.uniform(-1.0, +1.0) / SCALE for _ in range(2)]

        # Main engine
        m_power = 0
        try:
            angle_is_normal = self.lander.angle <= (math.pi / 2) and self.lander.angle > (-math.pi / 2)
            if (action[0] > 0.0 and angle_is_normal):
                # Limits
                m_power = (np.clip(action[0], 0.0, 1.0) + 1.0) * 0.3  # 0.3..1.6
                assert m_power >= 0.3 and m_power <= 1.0
                # ------------------------------------------------------------------------
                ox = sin * (4 / SCALE + 2 * dispersion[0]) - cos * dispersion[
                    1]  # 4 is move a bit downwards, +-2 for randomness
                oy = -cos * (4 / SCALE + 2 * dispersion[0]) - sin * dispersion[1]
                impulse_pos = (rocketPart.position[0] + ox, rocketPart.position[1] + oy)

                # rocketParticles are just a decoration, 3.5 is here to make rocketParticle speed adequate
                p = self._create_particle(3.5, impulse_pos[0], impulse_pos[1], m_power,
                                          radius=7)

                rocketParticleImpulse = (ox * MAIN_ENGINE_POWER * m_power, oy * MAIN_ENGINE_POWER * m_power)
                bodyImpulse = (-ox * MAIN_ENGINE_POWER * m_power, -oy * MAIN_ENGINE_POWER * m_power)
                point = impulse_pos
                wake = True

                # Force instead of impulse. This enables proper scaling and values in Newtons
                p.ApplyForce(rocketParticleImpulse, point, wake)
                rocketPart.ApplyForce(bodyImpulse, point, wake)
        except:
            print("Error in main engine power.")

        return m_power

    def __side_engines_force_computation(self, action):
        # ----------------------------------------------------------------------------
        # Side engines
        dispersion = [self.np_random.uniform(-1.0, +1.0) / SCALE for _ in range(2)]
        sin = math.sin(self.lander.angle)  # for readability
        cos = math.cos(self.lander.angle)
        s_power = 0.0
        y_dir = 1  # Positioning for the side Thrusters
        engine_dir = 0
        if (self.settings['Side Engines']):  # Check if side gas thrusters are enabled
            if (np.abs(action[1]) > 0.5):  # Have to be > 0.5
                # Orientation engines
                engine_dir = np.sign(action[1])
                s_power = np.clip(np.abs(action[1]), 0.5, 1.0)
                assert s_power >= 0.5 and s_power <= 1.0

                # if (self.lander.worldCenter.y > self.lander.position[1]):
                #     y_dir = 1
                # else:
                #     y_dir = -1

                # Positioning
                constant = (LANDER_LENGTH - SIDE_ENGINE_VERTICAL_OFFSET) / SCALE
                dx_part1 = - sin * constant  # Used as reference for dy
                dx_part2 = - cos * engine_dir * SIDE_ENGINE_AWAY / SCALE
                dx = dx_part1 + dx_part2

                dy = np.sqrt(
                    np.square(constant) - np.square(dx_part1)) * y_dir - sin * engine_dir * SIDE_ENGINE_AWAY / SCALE

                # Force magnitude
                oy = -cos * dispersion[0] - sin * (3 * dispersion[1] + engine_dir * SIDE_ENGINE_AWAY / SCALE)
                ox = sin * dispersion[0] - cos * (3 * dispersion[1] + engine_dir * SIDE_ENGINE_AWAY / SCALE)

                # Impulse Position
                impulse_pos = (self.lander.position[0] + dx,
                               self.lander.position[1] + dy)

                # Plotting purposes only
                self.impulsePos = (self.lander.position[0] + dx, self.lander.position[1] + dy)

                try:
                    p = self._create_particle(1, impulse_pos[0], impulse_pos[1], s_power, radius=3)
                    p.ApplyForce((ox * SIDE_ENGINE_POWER * s_power, oy * SIDE_ENGINE_POWER * s_power), impulse_pos,
                                 True)
                    self.lander.ApplyForce((-ox * SIDE_ENGINE_POWER * s_power, -oy * SIDE_ENGINE_POWER * s_power),
                                           impulse_pos, True)
                except:
                    logging.error("Error due to Nan in calculating y during sqrt(l^2 - x^2). "
                                  "x^2 > l^2 due to approximations on the order of approximately 1e-15.")

        return s_power, engine_dir

    def __generate_state(self):
        # ----------------------------------------------------------------------------
        # Update
        self.world.Step(1.0 / FPS, 6 * 30, 6 * 30)

        pos = self.lander.position
        vel = self.lander.linearVelocity

        # self.lander.angle = math.pi * 3
        # self.lander.position = (7, 12)
        state = [
            (pos.x - targetX) / (W / 2),
            (pos.y - (self.bargeHeight + (LEG_DOWN / SCALE))) / (H / 2) - LANDING_VERTICAL_CALIBRATION,
            # affects controller
            # self.bargeHeight includes height of helipad
            vel.x * (W / 2) / FPS,
            vel.y * (H / 2) / FPS,
            self.lander.angle,
            # self.nozzle.angle,
            20.0 * self.lander.angularVelocity / FPS,
            1.0 if self.legs[0].ground_contact else 0.0,
            1.0 if self.legs[1].ground_contact else 0.0
        ]
        untransformed_state = [pos.x, pos.y, vel.x, vel.y, self.lander.angle, self.lander.angularVelocity]

        return state, untransformed_state

    # ['dx','dy','x_vel','y_vel','theta','theta_dot','left_ground_contact','right_ground_contact']
    def __compute_rewards(self, state, main_engine_power, side_engine_power, part_angle):
        reward = 0
        shaping = -2000 * np.sqrt(np.square(state[0]) + np.square(state[1])) \
                  - 10 * np.sqrt(np.square(state[2]) + np.square(state[3])) \
                  - 1000 * abs(state[4]) - 30 * abs(state[5]) \
                  + 20 * state[6] + 20 * state[7]

        if state[3] > 0:
            shaping = shaping - 1

        if self.prev_shaping is not None:
            reward = shaping - self.prev_shaping
        self.prev_shaping = shaping

        # penalize the use of engines
        reward += -main_engine_power * 0.3
        if self.settings['Side Engines']:
            reward += -side_engine_power * 0.3

        return reward / 10

    """ PROBLEM SPECIFIC - RENDERING and OBJECT CREATION"""

    def _create_rocket(self, initial_coordinates):
        self.initial_coordinates = initial_coordinates

        body_color = (1, 1, 1)

        initial_x, initial_y = initial_coordinates
        self.lander = self.world.CreateDynamicBody(
            position=(initial_x, initial_y),
            angle=0,
            fixtures=fixtureDef(
                shape=polygonShape(vertices=[(x / SCALE, y / SCALE) for x, y in LANDER_POLY]),
                density=5.0,
                friction=0.1,
                categoryBits=0x0010,
                maskBits=0x001,  # collide only with ground
                restitution=0.0)  # 0.99 bouncy
        )
        self.lander.color1 = body_color
        self.lander.color2 = (0, 0, 0)

        if isinstance(self.settings['Initial Force'], str):
            self.lander.ApplyForceToCenter((
                self.np_random.uniform(-INITIAL_RANDOM * 0.3, INITIAL_RANDOM * 0.3),
                self.np_random.uniform(-1.3 * INITIAL_RANDOM, -INITIAL_RANDOM)
            ), True)
        else:
            self.lander.ApplyForceToCenter(self.settings['Initial Force'], True)

        # COG is set in the middle of the polygon by default. x = 0 = middle.
        # self.lander.mass = 25
        # self.lander.localCenter = (0, 3) # COG
        # ----------------------------------------------------------------------------------------
        # LEGS
        self.legs = []
        for i in [-1, +1]:
            leg = self.world.CreateDynamicBody(
                position=(initial_x - i * LEG_AWAY / SCALE, initial_y),
                angle=(i * 0.05),
                fixtures=fixtureDef(
                    shape=polygonShape(box=(LEG_W / SCALE, LEG_H / SCALE)),
                    density=5.0,
                    restitution=0.0,
                    categoryBits=0x0020,
                    maskBits=0x005)
            )
            leg.ground_contact = False
            leg.color1 = body_color
            leg.color2 = (0, 0, 0)
            rjd = revoluteJointDef(
                bodyA=self.lander,
                bodyB=leg,
                localAnchorA=(-i * 0.3 / LANDER_CONSTANT, 0),
                localAnchorB=(i * 0.5 / LANDER_CONSTANT, LEG_DOWN),
                enableMotor=True,
                enableLimit=True,
                maxMotorTorque=LEG_SPRING_TORQUE,
                motorSpeed=+0.3 * i  # low enough not to jump back into the sky
            )
            if i == -1:
                rjd.lowerAngle = 40 * DEGTORAD
                rjd.upperAngle = 45 * DEGTORAD
            else:
                rjd.lowerAngle = -45 * DEGTORAD
                rjd.upperAngle = -40 * DEGTORAD
            leg.joint = self.world.CreateJoint(rjd)
            self.legs.append(leg)
        # ----------------------------------------------------------------------------------------
        # NOZZLE
        self.nozzle = self.world.CreateDynamicBody(
            position=(initial_x, initial_y),
            angle=0.0,
            fixtures=fixtureDef(
                shape=polygonShape(vertices=[(x / SCALE, y / SCALE) for x, y in NOZZLE_POLY]),
                density=5.0,
                friction=0.1,
                categoryBits=0x0040,
                maskBits=0x003,  # collide only with ground
                restitution=0.0)  # 0.99 bouncy
        )
        self.nozzle.color1 = (0, 0, 0)
        self.nozzle.color2 = (0, 0, 0)
        rjd = revoluteJointDef(
            bodyA=self.lander,
            bodyB=self.nozzle,
            localAnchorA=(0, 0),
            localAnchorB=(0, 0.2),
            enableMotor=True,
            enableLimit=True,
            maxMotorTorque=NOZZLE_TORQUE,
            motorSpeed=0,
            referenceAngle=0,
            lowerAngle=-13 * DEGTORAD,  # +- 15 degrees limit applied in practice
            upperAngle=13 * DEGTORAD
        )
        # The default behaviour of a revolute joint is to rotate without resistance.
        self.nozzle.joint = self.world.CreateJoint(rjd)
        # ----------------------------------------------------------------------------------------
        # self.drawlist = [self.nozzle] + [self.lander] + self.legs
        self.drawlist = self.legs + [self.nozzle] + [self.lander]
        self.initial_mass = self.lander.mass
        self.remaining_fuel = INITIAL_FUEL_MASS_PERCENTAGE * self.initial_mass
        return

    # Problem specific - LINKED
    def _create_barges(self):
        self.left_barge = self.world.CreateStaticBody(
            fixtures=fixtureDef(shape=polygonShape(vertices=left_const_barge_coordinates_edges))
        )
        self.right_barge = self.world.CreateStaticBody(
            fixtures=fixtureDef(shape=polygonShape(vertices=right_const_barge_coordinates_edges))
        )

    def _create_particle(self, mass, x, y, ttl, radius=3):
        """
        Used for both the Main Engine and Side Engines
        :param mass: Different mass to represent different forces
        :param x: x position
        :param y:  y position
        :param ttl:
        :param radius:
        :return:
        """
        p = self.world.CreateDynamicBody(
            position=(x, y),
            angle=0.0,
            fixtures=fixtureDef(
                shape=circleShape(radius=radius / SCALE, pos=(0, 0)),
                density=mass,
                friction=0.1,
                categoryBits=0x0100,
                maskBits=0x001,  # collide only with ground
                restitution=0.3)
        )
        p.ttl = ttl  # ttl is decreased with every time step to determine if the particle should be destroyed
        self.particles.append(p)
        # Check if some particles need cleaning
        self._clean_particles(False)
        return p

    def _clean_particles(self, all_particles):
        while self.particles and (all_particles or self.particles[0].ttl < 0):
            self.world.DestroyBody(self.particles.pop(0))

    def _decrease_mass(self, main_engine_power, side_engine_power):
        x = np.array([float(main_engine_power), float(side_engine_power)])
        consumed_fuel = 0.009 * np.sum(x * (MAIN_ENGINE_FUEL_COST, SIDE_ENGINE_FUEL_COST)) / SCALE
        self.lander.mass = self.lander.mass - consumed_fuel
        self.remaining_fuel -= consumed_fuel
        if self.remaining_fuel < 0:
            self.remaining_fuel = 0

    @staticmethod
    def _create_labels(labels):
        labels_dict = {}
        y_spacing = 0
        for text in labels:
            labels_dict[text] = pyglet.text.Label(text, font_size=15, x=W / 2, y=H / 2,  # - y_spacing*H/10,
                                                  anchor_x='right', anchor_y='center', color=(0, 255, 0, 255))
            y_spacing += 1
        return labels_dict

    """ RENDERING """

    def _render(self, mode='rgb_array'):
        self._render_environment([left_const_barge_coordinates_edges, right_const_barge_coordinates_edges])
        self._render_lander()
        self.draw_marker(x=self.lander.worldCenter.x, y=self.lander.worldCenter.y)  # Center of Gravity
        self.draw_marker(x=self.landing_coordinates[0], y=self.landing_coordinates[1])

        # return self.viewer.render(return_rgb_array=mode == 'rgb_array')

    def render(self, mode='rgb_array'):
        return self._render(mode)

    def refresh(self, mode='human', render=False):
        """
        Used instead of _render in order to draw user defined drawings from controllers, e.g. trajectories
        for the MPC or a a marking e.g. Center of Gravity
        :param mode:
        :param render:
        :return: Viewer
        """
        # Viewer Creation
        if self.viewer is None:  # Initial run will enter here
            self.viewer = rendering.Viewer(VIEWPORT_W, VIEWPORT_H)
            self.viewer.set_bounds(0, W, 0, H)

        if render:
            self.render('human')
        return self.viewer.render(return_rgb_array=mode == 'rgb_array')

    def _render_lander(self):
        # --------------------------------------------------------------------------------------------------------------
        # Rocket Lander
        # --------------------------------------------------------------------------------------------------------------
        # Lander and Particles
        for obj in self.particles + self.drawlist:
            for f in obj.fixtures:
                trans = f.body.transform
                if type(f.shape) is circleShape:
                    t = rendering.Transform(translation=trans * f.shape.pos)
                    self.viewer.draw_circle(f.shape.radius, 20, color=obj.color1).add_attr(t)
                    self.viewer.draw_circle(f.shape.radius, 20, color=obj.color2, filled=False, linewidth=2).add_attr(t)
                else:
                    # Lander
                    path = [trans * v for v in f.shape.vertices]
                    self.viewer.draw_polygon(path, color=obj.color1)
                    path.append(path[0])
                    self.viewer.draw_polyline(path, color=obj.color2, linewidth=2)

    def _update_particles(self):
        for obj in self.particles:
            obj.ttl -= 0.1
            obj.color1 = (max(0.2, 0.2 + obj.ttl), max(0.2, 0.5 * obj.ttl), max(0.2, 0.5 * obj.ttl))
            obj.color2 = (max(0.2, 0.2 + obj.ttl), max(0.2, 0.5 * obj.ttl), max(0.2, 0.5 * obj.ttl))

        self._clean_particles(False)

    def _render_environment(self, barges):
        # --------------------------------------------------------------------------------------------------------------
        # ENVIRONMENT
        # --------------------------------------------------------------------------------------------------------------
        for barge in barges:
            self.viewer.draw_polygon(barge, color=(0.4, 0.4, 0.4))
        # for g in self.ground_polys:
        #     self.viewer.draw_polygon(g, color=(0, 0.5, 1.0))
        # --------------------------------------------------------------------------------------------------------------

    """ CALLABLE DURING RUNTIME """

    def draw_marker(self, x, y):
        """
        Draws a black '+' sign at the x and y coordinates.
        :param x: normalized x position (0-1)
        :param y: normalized y position (0-1)
        :return:
        """
        offset = 0.2
        self.viewer.draw_polyline([(x, y - offset), (x, y + offset)], linewidth=2)
        self.viewer.draw_polyline([(x - offset, y), (x + offset, y)], linewidth=2)

    def draw_polygon(self, color=(0.2, 0.2, 0.2), **kwargs):
        # path expected as (x,y)
        if self.viewer is not None:
            path = kwargs.get('path')
            if path is not None:
                self.viewer.draw_polygon(path, color=color)
            else:
                x = kwargs.get('x')
                y = kwargs.get('y')
                self.viewer.draw_polygon([(xx, yy) for xx, yy in zip(x, y)], color=color)

    def draw_line(self, x, y, color=(0.2, 0.2, 0.2)):
        self.viewer.draw_polyline([(xx, yy) for xx, yy in zip(x, y)], linewidth=2, color=color)

    def get_landing_coordinates(self):
        x = (self.landing_barge_coordinates[1][0] - self.landing_barge_coordinates[0][0]) / 2 + \
            self.landing_barge_coordinates[0][0]
        y = abs(self.landing_barge_coordinates[2][1] - self.landing_barge_coordinates[3][1]) / 2 + \
            min(self.landing_barge_coordinates[2][1], self.landing_barge_coordinates[3][1])
        return [x, y]

    def get_barge_top_edge_points(self):
        return flatten_array(self.landing_barge_coordinates[2:])

    def get_state_with_barge_and_landing_coordinates(self, untransformed_state=False):
        if untransformed_state:
            state = self.untransformed_state
        else:
            state = self.state
        return flatten_array([state, [self.remaining_fuel,
                                      self.lander.mass],
                              self.get_barge_top_edge_points(),
                              self.get_landing_coordinates()])

    def apply_random_x_disturbance(self, epsilon, left_or_right, x_force=2000):
        if np.random.rand() < epsilon:
            if left_or_right:
                self.apply_disturbance('random', x_force, 0)
            else:
                self.apply_disturbance('random', -x_force, 0)

    def apply_random_y_disturbance(self, epsilon, y_force=2000):
        if np.random.rand() < epsilon:
            self.apply_disturbance('random', 0, -y_force)

    def apply_disturbance(self, force, *args):
        if force is not None:
            if isinstance(force, str):
                x, y = args
                self.lander.ApplyForceToCenter((
                    self.np_random.uniform(x),
                    self.np_random.uniform(y)
                ), True)
            elif isinstance(force, tuple):
                self.lander.ApplyForceToCenter(force, True)


def get_state_sample(samples, normal_state=True, untransformed_state=True):
    simulation_settings = {'Side Engines': True,
                           'Clouds': False,
                           'Vectorized Nozzle': True,
                           'Graph': False,
                           'Render': False,
                           'Starting Y-Pos Constant': 1,
                           'Initial Force': 'random',
                           'Rows': 1,
                           'Columns': 2}
    env = RocketLander(simulation_settings)
    env.reset()
    state_samples = []
    while len(state_samples) < samples:
        f_main = np.random.uniform(0, 1)
        f_side = np.random.uniform(-1, 1)
        psi = np.random.uniform(-90 * DEGTORAD, 90 * DEGTORAD)
        action = [f_main, f_side, psi]
        s, r, done, info = env.step(action)
        if normal_state:
            state_samples.append(s)
        else:
            state_samples.append(
                env.get_state_with_barge_and_landing_coordinates(untransformed_state=untransformed_state))
        if done:
            env.reset()
    env.close()
    return state_samples


def flatten_array(the_list):
    return list(chain.from_iterable(the_list))

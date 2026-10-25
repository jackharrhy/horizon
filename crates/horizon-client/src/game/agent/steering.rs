//! Route execution and gaze, separate from the grid search. No changes to player physics.
use super::{Game, Goto, yaw_pitch};
use horizon_client::collisions::SceneCollisions;
use horizon_core::geometry::Vec3;

#[derive(Default)]
pub(super) struct Steering {
    pub motion: Option<[f32; 2]>,
    tick: Option<u64>,
    yaw_speed: f32,
    pitch_speed: f32,
}

fn angle(delta: f32) -> f32 {
    (delta + 180.0).rem_euclid(360.0) - 180.0
}

fn turn(error: f32, velocity: &mut f32, rate: f32, acceleration: f32) -> f32 {
    let wanted = (error * 6.0).clamp(-rate, rate);
    *velocity += (wanted - *velocity).clamp(-acceleration * 0.05, acceleration * 0.05);
    let step = *velocity * 0.05;
    if step.signum() == error.signum() && step.abs() >= error.abs() {
        *velocity = 0.0;
        error
    } else {
        step
    }
}

impl Steering {
    fn look(&mut self, tick: u64, yaw: &mut f32, pitch: &mut f32, target: (f32, f32)) {
        if self.tick == Some(tick) {
            return;
        }
        if self
            .tick
            .is_none_or(|previous| tick.saturating_sub(previous) > 2)
        {
            self.yaw_speed = 0.0;
            self.pitch_speed = 0.0;
        }
        self.tick = Some(tick);
        *yaw += turn(angle(target.0 - *yaw), &mut self.yaw_speed, 180.0, 720.0);
        *pitch += turn(target.1 - *pitch, &mut self.pitch_speed, 90.0, 360.0);
        *pitch = pitch.clamp(-90.0, 90.0);
    }
}

/// A world-space travel direction expressed as the player's controller-stick input.
fn movement(travel: f32, gaze: f32, speed: f32) -> [f32; 2] {
    let delta = angle(travel - gaze).to_radians();
    [-delta.sin() * speed, delta.cos() * speed]
}

impl Game<'_> {
    pub(super) fn look_bot(&mut self, at: [f64; 3], travel: Option<f32>) {
        let Some(eye) = self.eye_position() else {
            return;
        };
        let (mut yaw, pitch) = yaw_pitch(eye, at);
        if let Some(travel) = travel {
            yaw = travel + angle(yaw - travel).clamp(-30.0, 30.0);
        }
        self.agent.steering.look(
            self.ticks,
            &mut self.yaw,
            &mut self.pitch,
            (yaw, pitch.clamp(-35.0, 35.0)),
        );
    }

    pub(super) fn drive_bot(
        &mut self,
        aim: [f64; 3],
        remaining: f64,
        sprint: bool,
        gaze: Option<[f64; 3]>,
    ) {
        let p = self.position();
        let travel = (-(aim[0] - p[0])).atan2(aim[2] - p[2]).to_degrees() as f32;
        self.look_bot(
            gaze.unwrap_or([aim[0], aim[1] + 1.25, aim[2]]),
            Some(travel),
        );
        // Turn before reversing, rather than walking backwards into unseen terrain.
        let speed = if (aim[0] - p[0]).hypot(aim[2] - p[2]) < 0.01
            || angle(travel - self.yaw).abs() > 75.0
        {
            0.0
        } else {
            (remaining / 1.6).clamp(0.25, 1.0) as f32
        };
        self.agent.steering.motion = Some(movement(travel, self.yaw, speed));
        if sprint && speed > 0.95 && angle(travel - self.yaw).abs() < 20.0 {
            self.keys.insert(winit::keyboard::KeyCode::ControlLeft);
        } else {
            self.keys.remove(&winit::keyboard::KeyCode::ControlLeft);
        }
    }

    /// Only shortcut level, fully supported walking. Steps/drops retain their planned nodes.
    pub(super) fn clear_walk_segment(&self, to: [f64; 3]) -> bool {
        let Some(player) = &self.player else {
            return false;
        };
        let from = self.position();
        if !player.ground.on_ground
            || (from[1] - to[1]).abs() > 0.03
            || (from[1] - from[1].round()).abs() > 0.03
        {
            return false;
        }
        let dx = to[0] - from[0];
        let dz = to[2] - from[2];
        let distance = dx.hypot(dz);
        if distance > 48.0 {
            return false;
        }
        let steps = (distance / 0.15).ceil().max(1.0) as usize;
        // Inflation covers the space between samples as well as the player's width.
        let bounds = player.bounds().inflate(Vec3::new(0.08, 0.0, 0.08));
        let mut collisions = SceneCollisions {
            world: &self.world,
            binding: self.collision,
            subject: None,
        };
        for step in 0..=steps {
            let t = step as f64 / steps as f64;
            let x = from[0] + dx * t;
            let z = from[2] + dz * t;
            let y = from[1].round() as i32;
            for ox in [-0.38, 0.38] {
                for oz in [-0.38, 0.38] {
                    let (bx, bz) = ((x + ox).floor() as i32, (z + oz).floor() as i32);
                    let ground = self.res.facts().get(self.world.block(bx, y - 1, bz));
                    let feet = self.res.facts().get(self.world.block(bx, y, bz));
                    if !self.world.chunks.contains_key(&(bx >> 4, bz >> 4))
                        || !ground.full_collision
                        || feet.fluid != horizon_client::blocks::Fluid::None
                    {
                        return false;
                    }
                }
            }
            if !collisions
                .unobstructed(bounds.move_by(Vec3::new(dx * t, 0.0, dz * t)))
                .unwrap_or(false)
            {
                return false;
            }
        }
        true
    }

    pub(super) fn bot_waypoint(&self, route: &mut Goto, fallback: [f64; 3]) -> [f64; 3] {
        if let Some((tick, aim)) = route.shortcut
            && tick == self.ticks
        {
            return aim;
        }
        let mut aim = fallback;
        if self.clear_walk_segment(route.target) {
            aim = route.target;
            route.next = route.path.len();
        } else {
            for index in (route.next..route.path.len()).rev() {
                let node = route.path[index];
                let point = [
                    f64::from(node[0]) + 0.5,
                    f64::from(node[1]),
                    f64::from(node[2]) + 0.5,
                ];
                // Bound failed shortcut searches around obstacles to a local look-ahead.
                if (point[0] - self.position()[0]).hypot(point[2] - self.position()[2]) <= 8.0
                    && self.clear_walk_segment(point)
                {
                    aim = point;
                    route.next = index;
                    break;
                }
            }
        }
        if self.player.as_ref().is_some_and(|p| p.ground.on_ground)
            && (aim[1] - self.position()[1]).abs() < 0.03
            && !self.clear_walk_segment(aim)
        {
            aim = self.position();
        }
        route.shortcut = Some((self.ticks, aim));
        aim
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn bounded_turns_wrap_and_do_not_depend_on_poll_frequency() {
        let mut steering = Steering::default();
        let (mut yaw, mut pitch) = (179.0, 0.0);
        steering.look(1, &mut yaw, &mut pitch, (-150.0, 20.0));
        assert!(yaw > 179.0 && yaw - 179.0 <= 1.81);
        let once = (yaw, pitch);
        for _ in 0..10 {
            steering.look(1, &mut yaw, &mut pitch, (-150.0, 20.0));
        }
        assert_eq!((yaw, pitch), once);
        for tick in 2..80 {
            let previous = (yaw, pitch);
            steering.look(tick, &mut yaw, &mut pitch, (-150.0, 20.0));
            assert!((yaw - previous.0).abs() <= 9.01);
            assert!((pitch - previous.1).abs() <= 4.51);
        }
        assert!(angle(yaw + 150.0).abs() < 0.01 && (pitch - 20.0).abs() < 0.01);
    }

    #[test]
    fn travel_is_independent_of_look_direction() {
        for travel in [-180.0_f32, -90.0, 0.0, 45.0, 90.0] {
            for gaze in [-90.0_f32, 0.0, 30.0, 170.0] {
                let [left, forward] = movement(travel, gaze, 1.0);
                let yaw = gaze.to_radians();
                let x = left * yaw.cos() - forward * yaw.sin();
                let z = left * yaw.sin() + forward * yaw.cos();
                assert!((x + travel.to_radians().sin()).abs() < 1e-5);
                assert!((z - travel.to_radians().cos()).abs() < 1e-5);
            }
        }
    }
}

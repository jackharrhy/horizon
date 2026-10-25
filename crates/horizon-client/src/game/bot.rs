//! Client progression is independent of image production in `--bot` mode.
use super::{Game, Screen};
use horizon_client::Result;
use serde_json::{Value, json};
use std::time::{Duration, Instant};
use winit::keyboard::KeyCode;

pub(super) struct Presentation {
    pub live: bool,
    pub pumps: u64,
    pub next: Instant,
    pub next_picture: Instant,
    pub previous_picture: Instant,
    pub capture: Option<Value>,
    pub broadcast: Option<super::broadcast::Broadcast>,
}

impl Default for Presentation {
    fn default() -> Self {
        Self {
            live: false,
            pumps: 0,
            next: Instant::now(),
            next_picture: Instant::now(),
            previous_picture: Instant::now(),
            capture: None,
            broadcast: None,
        }
    }
}

pub(super) fn next_picture_deadline(
    previous: Instant,
    now: Instant,
    interval: Duration,
) -> Instant {
    let mut next = previous + interval;
    if next <= now {
        let skipped = now.duration_since(next).as_nanos() / interval.as_nanos() + 1;
        next += interval * skipped.min(u128::from(u32::MAX)) as u32;
    }
    next
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn presentation_deadlines_do_not_drift_or_burst_after_a_slow_frame() {
        let start = Instant::now();
        let interval = Duration::from_millis(10);
        assert_eq!(
            next_picture_deadline(start, start + Duration::from_millis(3), interval),
            start + interval
        );
        assert_eq!(
            next_picture_deadline(start, start + Duration::from_millis(37), interval),
            start + Duration::from_millis(40)
        );
    }
}

impl Game<'_> {
    pub(super) fn input_revision(&self) -> u64 {
        if self.options.bot {
            self.bot.pumps
        } else {
            self.frames
        }
    }

    /// Same gameplay path for ordinary frames and demand-rendered clients.
    pub(super) fn advance_client(&mut self, elapsed: Duration) -> Result<Instant> {
        if self.screen == Screen::Playing
            && self.title.is_none()
            && self.sign_edit.is_none()
            && self.book.is_none()
        {
            let turn = |k| f32::from(u8::from(self.keys.contains(&k)));
            let secs = elapsed.as_secs_f32().min(0.1);
            self.yaw += (turn(KeyCode::ArrowRight) - turn(KeyCode::ArrowLeft)) * 90.0 * secs;
            self.pitch = (self.pitch
                + (turn(KeyCode::ArrowDown) - turn(KeyCode::ArrowUp)) * 90.0 * secs)
                .clamp(-90.0, 90.0);
        }
        if self.title.is_none() {
            horizon_client::spans::time("update_world", || self.update_world());
            horizon_client::spans::time("spawn_player", || self.spawn_player())?;
        }
        let updated = Instant::now();
        self.track_toggle_screens();
        horizon_client::heartbeat::set_lan(self.is_published());
        let steps = self.clock.advance(elapsed);
        let in_frame = self.remote.is_none();
        let mut spent = Duration::ZERO;
        for step in 0..steps {
            if in_frame && !self.clock.tick_fits(spent) {
                if let Some(warning) = self.clock.drop_ticks(steps - step) {
                    log::warn!("{warning}");
                }
                break;
            }
            let started = Instant::now();
            if self.options.bot {
                // A delayed picture may batch ticks. Input deadlines must still
                // be applied between ticks, not only on the next event-loop poll.
                self.advance_agent();
            }
            if self.title.is_none() && !self.pause {
                let tick = Instant::now();
                horizon_client::spans::time("tick", || self.tick())?;
                if self.remote.is_none() {
                    self.perf.tick(tick.elapsed().as_secs_f32() * 1000.0);
                    self.debug_charts.log_tick(tick, tick.elapsed());
                }
                self.tick_ambience();
                self.tick_fov();
            }
            self.tick_win_screen()?;
            self.tick_music();
            self.tick_sound_engine();
            self.clock.record(started.elapsed());
            spent += started.elapsed();
            if let Some(report) = self.clock.take_sprint_report() {
                self.sprint_report(&report);
            }
        }
        self.extras_frame(elapsed);
        self.mods_frame(elapsed);
        self.extras_retry()?;
        self.apply_mouse_look();
        self.pad_look(elapsed.as_secs_f32());
        self.advance_flight();
        Ok(updated)
    }

    pub(super) fn pump_bot(&mut self) -> Result<()> {
        let now = Instant::now();
        let elapsed = now.duration_since(self.previous);
        self.previous = now;
        self.frame_ticks = elapsed.as_secs_f32() * 20.0;
        self.advance_client(elapsed)?;
        if self.bot.broadcast.is_some()
            && let Some(eye) = self.eye_position()
        {
            self.audio.set_listener(eye, self.yaw, self.pitch);
        }
        self.pump_broadcast()?;
        self.pause = false;
        self.clock.end_frame(false);
        self.bot.pumps += 1;
        self.bot.next = Instant::now() + Duration::from_millis(10);
        Ok(())
    }

    pub(super) fn capture_metadata(&self) -> Value {
        json!({
            "tick": self.ticks,
            "dimension": self.dim.id(),
            "captured_at_seconds": self.started.elapsed().as_secs_f64(),
            "player": self.player.as_ref().map(|p| [p.position.x, p.position.y, p.position.z]),
            "yaw": self.yaw,
            "pitch": self.pitch,
            "pending_chunks": self.streaming.pending() + self.remote_backlog(),
            "pending_uploads": self.pending_uploads.len(),
            "source": "client-predicted state, not server-authoritative",
        })
    }
}

//! Bounded, asynchronous raw media output to a loopback encoder. Never captures the desktop.
use super::Game;
use horizon_client::Result;
use serde_json::{Value, json};
use std::{
    io::Write,
    net::{SocketAddr, TcpStream},
    sync::{
        Arc,
        atomic::{AtomicBool, AtomicU64, Ordering},
        mpsc::{SyncSender, sync_channel},
    },
    time::{Duration, Instant},
};

struct MediaPacket {
    header: Vec<u8>,
    bytes: Vec<u8>,
}

pub(super) struct Broadcast {
    pub fps: u32,
    audio: bool,
    sender: SyncSender<MediaPacket>,
    connected: Arc<AtomicBool>,
    sent: Arc<AtomicU64>,
    dropped: u64,
    started: Instant,
    audio_frames: u64,
}

impl Broadcast {
    fn connect(address: &str, fps: u32, audio: bool) -> Result<Self> {
        let address: SocketAddr = address.parse()?;
        if !address.ip().is_loopback() || !(1..=60).contains(&fps) {
            return Err("broadcast requires a loopback address and FPS in 1..60".into());
        }
        let mut stream = TcpStream::connect_timeout(&address, Duration::from_secs(2))?;
        stream.set_nodelay(true)?;
        stream.set_write_timeout(Some(Duration::from_millis(500)))?;
        let (sender, receiver) = sync_channel::<MediaPacket>(4);
        let connected = Arc::new(AtomicBool::new(true));
        let alive = connected.clone();
        let sent = Arc::new(AtomicU64::new(0));
        let counter = sent.clone();
        std::thread::Builder::new()
            .name("bot-media".into())
            .spawn(move || {
                for packet in receiver {
                    if !alive.load(Ordering::Relaxed)
                        || stream
                            .write_all(&packet.header)
                            .and_then(|()| stream.write_all(&packet.bytes))
                            .is_err()
                    {
                        break;
                    }
                    if packet.header[0] == 1 {
                        counter.fetch_add(1, Ordering::Relaxed);
                    }
                }
                alive.store(false, Ordering::Relaxed);
            })?;
        Ok(Self {
            fps,
            audio,
            sender,
            connected,
            sent,
            dropped: 0,
            started: Instant::now(),
            audio_frames: 0,
        })
    }

    fn packet(&mut self, kind: u8, header: &[u8], bytes: Vec<u8>) {
        let mut packet = Vec::with_capacity(5 + header.len());
        packet.push(kind);
        packet.extend_from_slice(&((header.len() + bytes.len()) as u32).to_le_bytes());
        packet.extend_from_slice(header);
        if self
            .sender
            .try_send(MediaPacket {
                header: packet,
                bytes,
            })
            .is_err()
        {
            self.dropped += 1;
        }
    }

    pub fn status(&self) -> Value {
        json!({ "fps": self.fps, "audio": self.audio, "connected": self.connected.load(Ordering::Relaxed),
            "frames": self.sent.load(Ordering::Relaxed), "dropped": self.dropped,
            "seconds": self.started.elapsed().as_secs_f64() })
    }
}

impl Drop for Broadcast {
    fn drop(&mut self) {
        self.connected.store(false, Ordering::Relaxed);
    }
}

impl Game<'_> {
    pub(super) fn broadcast_command(&mut self, words: &[&str]) -> Result<Value> {
        if !self.options.bot {
            return Err("broadcast requires --bot".into());
        }
        match words.get(1).copied() {
            Some("stop") => {
                self.bot.broadcast = None;
                self.audio.capture_output(false)?;
            }
            Some("audio") => {
                let enabled = match words.get(2).copied() {
                    Some("on") => true,
                    Some("off") => false,
                    _ => return Err("broadcast audio on|off".into()),
                };
                let broadcast = self.bot.broadcast.as_mut().ok_or("no active broadcast")?;
                self.audio.capture_output(enabled)?;
                broadcast.audio = enabled;
                broadcast.audio_frames = (broadcast.started.elapsed().as_secs_f64() * 48_000.0) as u64;
            }
            Some("start") => {
                let address = words
                    .get(2)
                    .ok_or("broadcast start 127.0.0.1:PORT FPS [audio]")?;
                let fps: u32 = words.get(3).ok_or("missing FPS")?.parse()?;
                let audio = words.get(4) == Some(&"audio");
                let broadcast = Broadcast::connect(address, fps, audio)?;
                self.audio.capture_output(audio)?;
                self.bot.broadcast = Some(broadcast);
                self.bot.next_picture = Instant::now();
            }
            None => {}
            _ => return Err("broadcast start 127.0.0.1:PORT FPS [audio] | broadcast audio on|off | broadcast stop".into()),
        }
        Ok(json!({ "ok": true, "broadcast": self.bot.broadcast.as_ref().map(Broadcast::status) }))
    }

    pub(super) fn pump_broadcast(&mut self) -> Result<()> {
        let frame = self
            .renderer
            .as_mut()
            .and_then(|renderer| renderer.poll_capture());
        let Some(broadcast) = self.bot.broadcast.as_mut() else {
            return Ok(());
        };
        if !broadcast.connected.load(Ordering::Relaxed) {
            self.bot.broadcast = None;
            self.audio.capture_output(false)?;
            return Ok(());
        }
        if let Some(([width, height], pixels, captured_at)) = frame
            && captured_at >= broadcast.started
        {
            let mut header = [0; 16];
            header[..4].copy_from_slice(&width.to_le_bytes());
            header[4..8].copy_from_slice(&height.to_le_bytes());
            header[8..].copy_from_slice(
                &(captured_at
                    .saturating_duration_since(broadcast.started)
                    .as_micros() as u64)
                    .to_le_bytes(),
            );
            broadcast.packet(1, &header, pixels);
        }
        if broadcast.audio {
            let target = (broadcast.started.elapsed().as_secs_f64() * 48_000.0) as u64;
            let count = target.saturating_sub(broadcast.audio_frames).min(48_000);
            if count > 0 {
                let start = target - count;
                broadcast.audio_frames = target;
                let samples = self.audio.capture_pcm(count as usize);
                let bytes: Vec<u8> = samples
                    .iter()
                    .flat_map(|sample| sample.to_le_bytes())
                    .collect();
                broadcast.packet(2, &start.to_le_bytes(), bytes);
            }
        }
        Ok(())
    }
}

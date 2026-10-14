#[derive(Debug, Clone, Default, PartialEq)]
pub struct Event {
    id: Option<u32>,
    sample_pos: Option<u64>,
    pos: Option<[f64; 3]>,
    gain_db: Option<i8>,
    /// Object spatial extent per axis (w, d, h), each in [0.0, 1.0].
    /// `None` indicates no size update in this event (preserve previous state).
    size: Option<[f64; 3]>,
    ramp_length: Option<u32>,
}

impl Event {
    pub fn with_id(id: u32) -> Self {
        Self {
            id: Some(id),
            ..Default::default()
        }
    }

    pub fn id(&self) -> Option<u32> {
        self.id
    }

    pub fn pos(&self) -> Option<&[f64]> {
        self.pos.as_ref().map(|p| p.as_slice())
    }

    pub fn gain_db(&self) -> Option<i8> {
        self.gain_db
    }

    pub fn size(&self) -> Option<[f64; 3]> {
        self.size
    }

    pub fn ramp_length(&self) -> Option<u32> {
        self.ramp_length
    }

    pub fn sample_pos(&self) -> Option<u64> {
        self.sample_pos
    }

    pub fn set_sample_pos(&mut self, pos: u64) {
        self.sample_pos = Some(pos);
    }

    pub fn set_pos(&mut self, pos: [f64; 3]) {
        self.pos = Some(pos);
    }

    pub fn set_gain_db(&mut self, gain: i8) {
        self.gain_db = Some(gain);
    }

    pub fn set_size(&mut self, size: [f64; 3]) {
        self.size = Some(size);
    }

    pub fn set_ramp_length(&mut self, len: u32) {
        self.ramp_length = Some(len);
    }
}

pub struct Configuration {
    pub events: Vec<Event>,
}

impl Configuration {
    pub fn new(events: Vec<Event>) -> Self {
        Self { events }
    }
}

// ---------------------------------------------------------------------------
// Conversions from ABI-stable bridge types
// ---------------------------------------------------------------------------

impl From<bridge_api::REvent> for Event {
    fn from(r: bridge_api::REvent) -> Self {
        let mut e = Event::with_id(r.id);
        e.set_sample_pos(r.sample_pos);
        e.set_gain_db(r.gain_db);
        e.set_ramp_length(r.ramp_duration);
        if r.has_pos {
            e.set_pos(r.pos);
            e.set_size(r.size);
        }
        e
    }
}

impl From<&bridge_api::RMetadataFrame> for Configuration {
    fn from(frame: &bridge_api::RMetadataFrame) -> Self {
        Self::new(frame.events.iter().cloned().map(Event::from).collect())
    }
}

// Port of src/safehalo/pet_filter.py (PetImmunityFilter)

pub struct PetResult {
    pub is_pet: bool,
    pub confidence: f64,
    pub spr: f64,
    pub energy_scale_ratio: f64,
}

pub struct PetFilter {
    spr_threshold: f64,
    min_motion_var: f64,
}

impl PetFilter {
    pub fn new() -> Self {
        PetFilter { spr_threshold: 0.45, min_motion_var: 1e-5 }
    }

    pub fn evaluate(
        &self,
        sc_variances: &[f64],
        base_variance: f64,
        diff_energy: f64,
        base_diff: f64,
    ) -> PetResult {
        if sc_variances.is_empty() {
            return PetResult { is_pet: false, confidence: 0.0, spr: 0.0, energy_scale_ratio: 0.0 };
        }
        let total = sc_variances.len();
        let active_threshold = self.min_motion_var.max(base_variance * 2.0);
        let active = sc_variances.iter().filter(|&&v| v > active_threshold).count();
        let spr = active as f64 / total as f64;
        let diff_ratio = if base_diff > 1e-6 { diff_energy / base_diff } else { 1.0 };

        let is_active_motion = diff_ratio >= 1.5;
        let is_low_participation = spr < self.spr_threshold;
        let is_moderate_energy = diff_ratio >= 1.5 && diff_ratio < 2.6;

        if is_active_motion && is_low_participation && is_moderate_energy {
            let confidence = ((1.0 - spr) * 0.7 + (1.0 - diff_ratio / 3.0) * 0.3)
                .max(0.4)
                .min(0.95);
            return PetResult {
                is_pet: true,
                confidence,
                spr,
                energy_scale_ratio: diff_ratio / 3.0,
            };
        }
        PetResult { is_pet: false, confidence: 0.0, spr, energy_scale_ratio: diff_ratio / 3.0 }
    }
}
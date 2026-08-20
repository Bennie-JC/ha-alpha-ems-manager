"""Does the measured state of charge agree with the measured battery power?

Instrumentation, and **only** instrumentation. Nothing here gates a command, and
that restraint is the point: the question is worth asking precisely because the
signal the control layer might otherwise have leaned on turned out to answer a
different one.

The energy-balance identity cannot help here. On an installation whose
house-load figure is derived from the inverter's own grid register while the
balance check reads a separate meter, the residual reduces to the difference
between those two meters -- the battery term cancels identically and the state of
charge never appears. So however large that residual grows, it says nothing about
the two readings a battery command actually depends on.

This does look at exactly those two, and asks the only question that constrains
them jointly: over one closed interval, did the stored energy move the way the
measured power said it would? A sensor that is stuck, mis-scaled or
sign-inverted fails it. A disagreement between two grid meters cannot, because
neither is involved.

It is not promoted to a gate yet, for one honest reason: the classifier's
resolution depends on how finely the state-of-charge sensor actually reports, and
that is a property of the installation rather than something to assume. So this
*measures* that resolution from the data it sees, which is the evidence a later
phase would need before trusting it with a veto.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .const import MAX_CONTROL_EVENTS_REPORTED

#: How the two figures compared over one interval.
COHERENCE_AGREE = "agree"
COHERENCE_DISAGREE = "disagree"
#: Neither figure moved enough to distinguish agreement from quantisation.
COHERENCE_INCONCLUSIVE = "inconclusive"

#: Quantisation steps a movement must clear before it is treated as real.
#:
#: Two, so a single step of rounding at each end of the interval cannot by itself
#: manufacture a movement or hide one.
_RESOLUTION_STEPS = 2

#: Assumed reporting step of a state-of-charge sensor, in percent.
#:
#: Sizes the band below which a comparison cannot conclude. The smallest movement
#: actually observed is reported *alongside* rather than substituted in, and the
#: reason is worth recording: a minimum-so-far is an upper bound on the true step,
#: and it is at its worst on the very first sample, where the only movement seen
#: is that sample's own. Sizing the band from it made a healthy three-percent
#: discharge inconclusive against a band of its own making.
#:
#: So the band stays fixed and the measurement stays evidence. If a sensor turns
#: out coarser than this, the reported figure is what says so -- and since nothing
#: here gates anything, an over-eager disagreement costs a diagnostics line rather
#: than a refused command.
_ASSUMED_SOC_STEP_PERCENT = 0.1


@dataclass(frozen=True, slots=True)
class SocCoherenceSample:
    """One closed interval's comparison.

    Deliberately stores only what was measured. Every judgement is derived, so a
    stored verdict cannot drift out of step with the numbers behind it -- the same
    shape the energy-balance sample and the battery plan both use.
    """

    #: Chronological interval index within the civil day.
    index: int
    soc_before_percent: float
    soc_after_percent: float
    #: Battery power at the close of the interval, positive for charging.
    battery_power_w: float
    #: Configured DC capacity, which converts a percentage into an energy.
    capacity_kwh: float
    interval_hours: float
    #: Smallest non-zero movement seen so far, when one has been. Reported as
    #: evidence about what this sensor can resolve; deliberately not used to size
    #: the band. See ``_ASSUMED_SOC_STEP_PERCENT``.
    observed_step_percent: float | None = None

    @property
    def soc_delta_percent(self) -> float:
        """Return the movement in percent, positive for charging."""
        return self.soc_after_percent - self.soc_before_percent

    @property
    def observed_dc_kwh(self) -> float:
        """Return the stored-energy change the state of charge implies."""
        return self.soc_delta_percent / 100.0 * self.capacity_kwh

    @property
    def expected_ac_kwh(self) -> float:
        """Return the energy the measured power implies, on the AC side."""
        return self.battery_power_w / 1000.0 * self.interval_hours

    @property
    def resolution_kwh(self) -> float:
        """Return the movement below which the comparison cannot conclude.

        Two quantisation steps of the assumed reporting resolution, so a single
        step of rounding at each end of the interval cannot manufacture a
        movement or hide one.

        Efficiency is deliberately **not** applied anywhere in this module: the
        two sides sit on opposite sides of the conversion boundary, and folding a
        configured efficiency in would turn a measurement into a partly modelled
        quantity. The comparison is kept to direction and order of magnitude,
        which needs no such figure.
        """
        return _RESOLUTION_STEPS * _ASSUMED_SOC_STEP_PERCENT / 100.0 * self.capacity_kwh

    @property
    def verdict(self) -> str:
        """Return whether the two figures agree, disagree, or cannot say."""
        limit = self.resolution_kwh
        observed = self.observed_dc_kwh
        expected = self.expected_ac_kwh
        if abs(observed) < limit and abs(expected) < limit:
            return COHERENCE_INCONCLUSIVE
        if abs(observed) < limit or abs(expected) < limit:
            # One side moved and the other did not. That is exactly the shape a
            # stuck sensor makes, so it is reported rather than excused.
            return COHERENCE_DISAGREE
        same_direction = (observed > 0.0) == (expected > 0.0)
        return COHERENCE_AGREE if same_direction else COHERENCE_DISAGREE

    def as_dict(self) -> dict[str, Any]:
        """Return the bounded diagnostics form."""
        return {
            "index": self.index,
            "soc_delta_percent": round(self.soc_delta_percent, 2),
            "battery_power_w": round(self.battery_power_w, 1),
            "observed_dc_kwh": round(self.observed_dc_kwh, 4),
            "expected_ac_kwh": round(self.expected_ac_kwh, 4),
            "resolution_kwh": round(self.resolution_kwh, 4),
            "verdict": self.verdict,
        }


@dataclass(slots=True)
class SocCoherenceMonitor:
    """Session-scoped tallies over the comparisons seen so far.

    Deliberately not persisted, for the same reason the energy-balance monitor is
    not: it is evidence about the running installation, and a topology question
    that deserves durable evidence should be answered with a deliberate schema
    change rather than by quietly widening this.
    """

    agree: int = 0
    disagree: int = 0
    inconclusive: int = 0
    #: Smallest non-zero movement observed, in percent. This is the figure worth
    #: watching: it says what the sensor can actually resolve, and therefore
    #: whether this comparison could ever carry a veto.
    observed_step_percent: float | None = None
    #: Most recent comparisons, newest first, bounded like every other list that
    #: reaches diagnostics.
    recent: list[SocCoherenceSample] = field(default_factory=list)

    @property
    def conclusive(self) -> int:
        """Return how many comparisons could distinguish agreement."""
        return self.agree + self.disagree

    @property
    def agreement_rate(self) -> float | None:
        """Return the share of conclusive comparisons that agreed."""
        if self.conclusive <= 0:
            return None
        return self.agree / self.conclusive

    def observe(
        self,
        *,
        index: int,
        soc_before_percent: float,
        soc_after_percent: float,
        battery_power_w: float,
        capacity_kwh: float,
        interval_hours: float,
    ) -> SocCoherenceSample | None:
        """Record one closed interval, and return the sample it produced.

        Returns ``None`` when the inputs cannot form a comparison at all, which
        is not the same as an inconclusive comparison: the first means there was
        nothing to compare, the second that there was and it was too small to
        read.
        """
        if capacity_kwh <= 0.0 or interval_hours <= 0.0:
            return None

        movement = abs(soc_after_percent - soc_before_percent)
        if movement > 0.0 and (
            self.observed_step_percent is None or movement < self.observed_step_percent
        ):
            self.observed_step_percent = movement

        sample = SocCoherenceSample(
            index=index,
            soc_before_percent=soc_before_percent,
            soc_after_percent=soc_after_percent,
            battery_power_w=battery_power_w,
            capacity_kwh=capacity_kwh,
            interval_hours=interval_hours,
            observed_step_percent=self.observed_step_percent,
        )

        verdict = sample.verdict
        if verdict == COHERENCE_AGREE:
            self.agree += 1
        elif verdict == COHERENCE_DISAGREE:
            self.disagree += 1
        else:
            self.inconclusive += 1

        self.recent.insert(0, sample)
        del self.recent[MAX_CONTROL_EVENTS_REPORTED:]
        return sample

    def as_dict(self) -> dict[str, Any]:
        """Return the bounded diagnostics form."""
        return {
            "agree": self.agree,
            "disagree": self.disagree,
            "inconclusive": self.inconclusive,
            "conclusive": self.conclusive,
            "agreement_rate": (
                None if self.agreement_rate is None else round(self.agreement_rate, 4)
            ),
            "observed_soc_step_percent": self.observed_step_percent,
            "recent": [sample.as_dict() for sample in self.recent],
            "basis": (
                "compares the stored-energy change the state of charge implies "
                "against the energy the measured battery power implies, over one "
                "closed interval; direction and magnitude only, no efficiency "
                "applied"
            ),
            "status": (
                "instrumentation only: this does not gate any command, and its "
                "resolution depends on what the state-of-charge sensor can "
                "actually report, which is measured here rather than assumed"
            ),
        }

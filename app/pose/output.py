from __future__ import annotations

import numpy as np

ACTION_KO = {
    "adjust": "자세 고치기",
    "fall": "넘어짐",
    "getup": "일어나기",
    "jump": "점프",
    "lie": "누워 있음",
    "lie_down": "눕기",
    "return": "제자리로 돌아오기",
    "slow_lie": "천천히 눕기",
    "stand": "서 있음",
    "two_steps": "두 걸음",
    "walk": "걷기",
    "other": "기타",
}
POSTURE_KO = {"upright": "서 있는 자세", "low": "낮은 자세·전환", "lying": "누운 자세", "uncertain": "판단 중"}
DIRECTION_KO = {"up": "상", "left": "좌", "right": "우"}


class _Hold:
    def __init__(self):
        self.candidate: str | None = None
        self.since: float | None = None

    def update(self, candidate: str | None, t: float, hold: float) -> str | None:
        if candidate != self.candidate:
            self.candidate, self.since = candidate, t
        if candidate is None or t - self.since + 1e-6 < hold:
            return None
        return candidate

    def reset(self) -> None:
        self.candidate, self.since = None, None


def _group_scores(prob: np.ndarray, classes: list[str], groups: dict[str, list[str]]) -> dict[str, float]:
    index = {c: i for i, c in enumerate(classes)}
    return {g: float(sum(prob[index[c]] for c in members if c in index)) for g, members in groups.items()}


class OutputShaper:
    def __init__(self, name: str, points: list[str], classes: list[str], display: dict, game: dict, fall: dict):
        self.name, self.points, self.classes = name, points, classes
        self.display, self.game, self.fall = display, game, fall
        self.hold = _Hold()
        self.last_t: float | None = None
        self.fall_since: float | None = None

    def reset(self) -> None:
        self.hold.reset()
        self.last_t = None
        self.fall_since = None

    def frame(self, t: float, xy: np.ndarray, smoothed: np.ndarray, prob: np.ndarray) -> dict:
        disp, game, classes = self.display, self.game, self.classes
        if self.last_t is not None and t - self.last_t > disp.get("reset_gap_sec", 0.6):
            self.hold.reset()
            self.fall_since = None
        self.last_t = t
        top = int(prob.argmax())
        groups = _group_scores(prob, classes, disp.get("groups", {}))
        ranked = sorted(groups.items(), key=lambda kv: kv[1], reverse=True)
        candidate = None
        if ranked:
            best, score = ranked[0]
            second = ranked[1][1] if len(ranked) > 1 else 0.0
            if score >= disp.get("min_score", 0.55) and score - second >= disp.get("min_margin", 0.10):
                candidate = best
        posture = self.hold.update(candidate, t, disp.get("hold_sec", 0.4)) or "uncertain"

        weights = game.get("intensity", {})
        activity = min(1.0, max(0.0, float(sum(p * weights.get(c, 0.0) for c, p in zip(classes, prob)))))
        direction = None
        for upper, code in game.get("directions", []):
            if activity <= upper:
                direction = code
                break

        fall_alarm = False
        if "fall" in classes and self.fall:
            if prob[classes.index("fall")] > self.fall.get("threshold", 0.7):
                self.fall_since = t if self.fall_since is None else self.fall_since
                fall_alarm = t - self.fall_since + 1e-6 >= self.fall.get("hold_sec", 0.4)
            else:
                self.fall_since = None

        def pts(arr: np.ndarray) -> list[dict]:
            return [{"name": n, "x": round(float(p[0]), 4), "y": round(float(p[1]), 4)} for n, p in zip(self.points, arr)]

        return {
            "type": "pose",
            "ts": round(t, 3),
            "model": self.name,
            "points": pts(xy),
            "smoothed": pts(smoothed),
            "action": {"code": classes[top], "label": ACTION_KO.get(classes[top], classes[top]),
                       "score": round(float(prob[top]), 4)},
            "probabilities": {c: round(float(p), 4) for c, p in zip(classes, prob)},
            "posture": {"code": posture, "label": POSTURE_KO[posture],
                        "scores": {g: round(s, 4) for g, s in groups.items()}},
            "activity_score": round(activity, 4),
            "direction": direction,
            "direction_ko": DIRECTION_KO.get(direction) if direction else None,
            "fall_alarm": fall_alarm,
        }

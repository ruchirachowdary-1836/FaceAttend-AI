"""Lightweight motion-based liveness check.

A printed photo or a phone screen held still produces almost no change in head pose, while a real
person naturally turns their head a little. We derive a yaw proxy from the five facial landmarks
(nose offset relative to the eye midpoint, divided by eye distance) and require its range across the
confirmation window to exceed a threshold. This stops casual replays; it is NOT a replacement for a
trained anti-spoofing model against a determined attacker (see README roadmap).
"""
import numpy as np


def yaw_proxy(kps):
    k = np.asarray(kps, dtype=np.float32)
    eyes_mid = (k[0] + k[1]) / 2
    eye_dist = np.linalg.norm(k[0] - k[1]) or 1.0
    return float((k[2][0] - eyes_mid[0]) / eye_dist)


def is_live(yaws, min_range):
    return len(yaws) >= 2 and (max(yaws) - min(yaws)) >= min_range

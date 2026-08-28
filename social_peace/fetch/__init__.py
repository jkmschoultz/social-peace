"""Optional 'top-up' fetchers that pull stock nature clips into assets/video for review.

These are a convenience layer over the curated library, not a runtime dependency of
the pipeline. Downloaded files land in a `_incoming/` subfolder so you can eyeball
and license-check them before moving them up into assets/video/.
"""

INCOMING_SUBDIR = "_incoming"

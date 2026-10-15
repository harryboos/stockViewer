"""One source of truth for forecast deadlines and the longer database lease."""

# Queue/rejection recovery must not consume an accepted MAX generation's time.
QUEUE_TIMEOUT_SECONDS = 180
MODEL_TIMEOUT_SECONDS = 1200
STREAM_IDLE_SECONDS = 180
DATA_TIMEOUT_SECONDS = 85
FEEDBACK_TIMEOUT_SECONDS = 5
NEWS_TIMEOUT_SECONDS = 25
# Includes collection, queueing, generation, validation and persistence headroom.
RUN_TIMEOUT_SECONDS = (DATA_TIMEOUT_SECONDS + FEEDBACK_TIMEOUT_SECONDS + NEWS_TIMEOUT_SECONDS
                       + QUEUE_TIMEOUT_SECONDS + MODEL_TIMEOUT_SECONDS + 35)
RUN_LEASE_SECONDS = RUN_TIMEOUT_SECONDS + 60

# Recommendations also stream MAX output; allow queue recovery before their
# shorter generation budget, then leave time to validate and save the result.
CONCEPT_MODEL_TIMEOUT_SECONDS = 480
CONCEPT_RUN_TIMEOUT_SECONDS = (DATA_TIMEOUT_SECONDS + NEWS_TIMEOUT_SECONDS
                               + QUEUE_TIMEOUT_SECONDS + CONCEPT_MODEL_TIMEOUT_SECONDS + 35)
CONCEPT_RUN_LEASE_SECONDS = CONCEPT_RUN_TIMEOUT_SECONDS + 60

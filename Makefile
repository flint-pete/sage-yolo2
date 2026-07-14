# sage-yolo2 -- Makefile
#
# `make test` runs the OFFLINE unit suite (consumer + save_match): pure-stdlib
# logic, no GPU / cv2 / YOLO. It self-bootstraps a throwaway venv with pytest so
# the suite runs out-of-the-box on a clean checkout.
#
# The GPU integration test (real YOLO inference) is separate: tests/run-tests.sh.

VENV := .venv-test
PY   := $(VENV)/bin/python

.PHONY: test clean

test: $(VENV)/.stamp
	$(PY) -m pytest -q tests/test_consumer.py tests/test_consumer_meta.py tests/test_identity.py tests/test_seenstore.py tests/test_selection.py tests/test_app_cache.py tests/test_save_match.py

$(VENV)/.stamp:
	python3 -m venv $(VENV)
	$(PY) -m pip install --quiet --upgrade pip
	$(PY) -m pip install --quiet pytest Pillow piexif numpy
	touch $@

clean:
	rm -rf $(VENV)

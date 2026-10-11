PYTHON ?= python3

.PHONY: check check-python check-public check-history check-scaffold hooks test smoke

check: check-python check-public check-history check-scaffold test

check-python:
	$(PYTHON) -c 'import sys; sys.exit("Python 3.11+ is required") if sys.version_info < (3, 11) else None'

check-public:
	$(PYTHON) scripts/check_public_content.py

# Every reachable commit: file versions, paths, messages and commit identities.
check-history:
	$(PYTHON) scripts/check_public_content.py --history

# Install the pre-push guard into this clone (hooks are not versioned, so run once per clone).
hooks:
	$(PYTHON) scripts/install_hooks.py

check-scaffold:
	$(PYTHON) scripts/check_scaffold.py

test:
	$(PYTHON) -m unittest discover -s tests -v

# The demo and its smoke check need aiohttp: run with a venv that has Roost installed.
smoke:
	$(PYTHON) dev/demo.py > /dev/null 2>&1 & pid=$$!; sleep 3; $(PYTHON) dev/smoke.py; status=$$?; kill $$pid; exit $$status

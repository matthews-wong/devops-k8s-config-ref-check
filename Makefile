.PHONY: validate schema refs test lint

# Created out of band, never committed; see README.
EXTERNAL := Secret/web-credentials

validate: schema refs test lint ## Run every local check

schema:
	kubeconform -strict -summary manifests/*.yaml

refs:
	scripts/check_refs.py $(addprefix --external ,$(EXTERNAL))

test:
	python3 -m unittest discover -s scripts -p 'test_*.py'

lint:
	actionlint

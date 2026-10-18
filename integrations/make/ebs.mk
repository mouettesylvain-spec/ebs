# Call EBS from an existing top-level Makefile:  include path/to/ebs.mk
#   make ebs-plan                      ebs plan
#   make ebs-build EBS_STEPS="lint"    ebs build (only these steps)
#   make ebs-fetch EBS_FETCH=summary   copy outputs into $(EBS_OUT)
# Make only forwards the request; EBS decides what actually runs.

EBS_FLOW  ?= flow.yaml
EBS_CACHE ?= read
EBS_STEPS ?=
EBS_FETCH ?= summary
EBS_OUT   ?= ebs-out

.PHONY: ebs-plan ebs-build ebs-status ebs-fetch

ebs-plan:
	ebs plan -f $(EBS_FLOW) $(EBS_STEPS)

ebs-build:
	ebs build -f $(EBS_FLOW) --cache $(EBS_CACHE) -k $(EBS_STEPS)

ebs-status:
	ebs status

ebs-fetch: ebs-build
	ebs fetch $(EBS_FETCH) -o $(EBS_OUT) --force

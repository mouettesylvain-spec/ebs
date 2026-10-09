# A lint flow as a team might already have it; ebs runs it unchanged (`kind: make`).
#   make lint BLOCK=alu               lint rtl/alu with the fake linter
#   make lint BLOCK=alu LINTER=verilator
# (reports/ is gitignored; ebs runs this in a scratch copy, never in your checkout)
# The report is normalized (sorted, relative paths, no dates), so equal findings give equal bytes.

BLOCK  ?= alu
LINTER ?= fake

RTL    := ../rtl/$(BLOCK)
SRCS   := $(sort $(wildcard $(RTL)/*.sv))
REPORT := reports/$(BLOCK).rpt

.PHONY: lint clean $(REPORT)  # always relint: never trust a report left in the workdir

lint: $(REPORT)
	@cat $(REPORT)

ifeq ($(LINTER),verilator)
$(REPORT): $(SRCS)
	@mkdir -p reports
	@verilator --lint-only -Wall -Wno-fatal --top-module $(BLOCK) $(SRCS) 2> reports/$(BLOCK).log \
	  || { cat reports/$(BLOCK).log >&2; exit 1; }  # warnings exit 0; errors fail the action
	@{ echo "lint report (verilator)"; \
	   grep -E '^%(Warning|Error)-[A-Z0-9]+: ' reports/$(BLOCK).log \
	     | sed -E 's#^%(Warning|Error)-([A-Z0-9]+): (\.\./)*([^:]+:[0-9]+):[0-9]+: #\4: \2: #' \
	     | LC_ALL=C sort -t: -k1,1 -k2,2n; \
	   echo "$$(grep -cE '^%(Warning|Error)-[A-Z0-9]+: ' reports/$(BLOCK).log) violation(s)"; \
	 } > $@
else ifeq ($(LINTER),fake)
$(REPORT): $(SRCS)
	@mkdir -p reports
	@bash ../tools/fake-lint $(SRCS) > $@
else
$(error LINTER must be 'fake' or 'verilator', not '$(LINTER)')
endif

clean:
	rm -rf reports

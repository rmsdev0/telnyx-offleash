"""Barge-in latency measurement harness for telnyx-offleash.

Option 1 (controlled harness): a scripted caller dials the live agent from a
second Telnyx number on the same Call Control connection, so both call legs land
in the same server process on one clock. The harness waits for the agent to
start speaking, injects a known barge-in stimulus at a recorded time, and
records the component timestamps needed to measure true barge-in latency.

See BENCHMARK.md for the method, the clock accounting, and the results.
"""

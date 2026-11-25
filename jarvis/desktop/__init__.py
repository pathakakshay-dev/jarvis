"""Desktop planning, safety checks, a mock simulator, and dedicated desktop actions.

The planner and the simulator do not control the desktop. The Notepad opener and
Calculator opener each start only their own fixed application after a separate
approval. Desktop control and window switching act on already open Notepad
windows after their own approvals. The Desktop Action Engine queues a short
sequence of those same safe actions and still requires one approval per step.
None of these use the planner, simulator, or goal agent, and none accept
arbitrary executable paths.
"""

"""Desktop planning, safety checks, a mock simulator, and dedicated desktop actions.

The planner and the simulator do not control the desktop. The Notepad opener and
Calculator opener each start only their own fixed application after a separate
approval. Desktop control can inspect, focus, or type into an already open
Notepad window after its own approval. Window switching can only focus an
already open Notepad window after its own approval. None of these use the
planner, simulator, or goal agent, and none accept arbitrary executable paths.
"""

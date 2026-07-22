"""Helios M6 insights: correlations, weekly review, doctor report, labs import.

Everything here is opt in. The heavy statistics live behind the optional
'insights' dependency group (scipy, numpy). Modules import cleanly without
scipy and degrade to self contained fallbacks so the daemon never crashes on
a machine that has not installed the extra group.
"""

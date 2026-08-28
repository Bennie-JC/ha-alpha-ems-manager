"""An offline replay of Stage-A decisions through more than one architecture.

Not a second optimiser. Every architecture here is the **production** solver with
a different reserve curve and a different terminal value, which is the only way a
comparison can mean anything: a simplified simulator would be answering a
different question and its verdict would be about the simulator.
"""

# The bootstrap: the compiled agent, or its source if that will not load.
# Wrapped so a failure drops to the REPL instead of reboot-looping.
import sys
try:
    try:
        import agent
    except Exception as exc:
        print("compiled agent would not load:", exc)
        sys.modules.pop("agent", None)
        import agent_src as agent
        sys.modules["agent"] = agent
        agent.BOOT_NOTE = "compiled agent would not load (%s); running from source" % exc
    agent.main()
except Exception as exc:
    print("agent did not start:", exc)

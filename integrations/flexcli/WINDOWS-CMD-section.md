### Architecture review of a C module (`/archcreator`, agent ArchCreator)

`/archcreator <Module> [question]` switches the session to the **ArchCreator** agent,
which runs **archcreator** on the module (the C file name without `.c`) and explains
the result. archcreator is a separate, deterministic tool: it parses the C sources,
builds the module's dependency model and checks the rules without any model.
The findings, severities and exit code are archcreator's; the agent quotes them,
reads the code at their `path:line`, and answers questions from archcreator's
`model.json`. It never creates, re-rates or drops a finding. Headless:

```bat
"%FLEXCLI_EXE%" run --standalone --agent arch-creator "Review DCDC_FSM and explain the complexity findings."
```

Install archcreator (see its README: a single `archcreator.exe` or a Python wheel)
outside the repository and either put it on `PATH` or set its full, absolute
path in `ARCHCREATOR_BIN`; an `archcreator` inside the project is never run, so a
checkout cannot plant one. Profiles (include paths, defines, build list, rule
thresholds) are kept centrally, not in the project: archcreator picks the one
whose `match` names the repository (git remote or folder name) from
`ARCHCREATOR_PROFILES`, else from the profiles shipped with it:

```bat
set "ARCHCREATOR_BIN=C:\tools\archcreator\archcreator.exe"
set "ARCHCREATOR_PROFILES=\\server\share\archcreator\profiles"
```

A call may name another profile to override; that asks you first, because a
profile decides what archcreator reports. The agent reads the `ARCHCREATOR_PROFILES`
folders without asking and anything else outside the project only with your
approval. archcreator parses only the module
and the *context* the agent passes (other modules, or all of them): callers,
signal producers and consumers outside that set are unknown to the run, and
the agent says which modules a run parsed. Asking "who calls this?" makes it
run again with the modules in question as context. Outputs go
to `.flexcli\archcreator\<session>\<Module>\` (`summary.json`, `report.md`,
`findings.json`, `model.json`), one folder per session, so parallel sessions
never read each other's run. Running archcreator and reading need no approval; the shell, MCP
tools and anything else not named ask first. The agent writes only under
`.flexcli\`, and uses no subagents or web access. What it reads (the summary,
report excerpts, source lines) goes to the model like any other tool result;
for a run without the model, call `archcreator analyze` directly. Gates and CI use
archcreator's exit code, not the agent.

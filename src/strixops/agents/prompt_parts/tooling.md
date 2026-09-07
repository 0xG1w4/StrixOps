TOOLING
exec_command runs inside the isolated sandbox container (Kali toolchain,
/workspace working dir); interactive programs need tty=true and write_stdin.
apply_patch creates, edits, or deletes sandbox files. Send the complete patch
text in its JSON command field; use paths relative to /workspace. Read files
with exec_command before editing, and inspect tool errors before retrying.
list_skills returns loadable canonical skill IDs and descriptions. Use those IDs
with load_skill or create_agent(skills=[...]); load the relevant playbook before
probing a surface class. Missing or ambiguous skills are explicit errors that must
be resolved before the dependent method starts. Tools list capabilities available
to the agent; skill text does not create new tools or verify remote access.

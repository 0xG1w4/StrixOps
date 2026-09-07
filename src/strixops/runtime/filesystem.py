"""Sandbox file editing and image inspection on Chat Completions routes.

The pinned SDK's SandboxApplyPatchTool is a freeform CustomTool. Keep its
parser and workspace editor, but expose JSON arguments on model routes that
only accept FunctionTool. All file operations stay on the bound session.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, Literal

from agents import FunctionTool, RunContextWrapper, function_tool
from agents.sandbox import Manifest
from agents.sandbox.capabilities import Capability
from agents.sandbox.capabilities.tools import ViewImageTool
from agents.sandbox.capabilities.tools.apply_patch_tool import SandboxApplyPatchTool
from pydantic import Field


def _patch_error(_ctx: RunContextWrapper[Any], error: Exception) -> str:
    return json.dumps(
        {
            "success": False,
            "error": f"{type(error).__name__}: {error}",
            "hint": "Check the current file contents and retry with a corrected patch in the command field.",
        },
        ensure_ascii=False,
    )


class SandboxFilesystem(Capability):
    """Bind JSON apply_patch and the SDK image reader to the run's sandbox."""

    type: Literal["filesystem"] = "filesystem"
    configure_tools: Callable[[list[FunctionTool]], list[FunctionTool]] | None = Field(
        default=None, exclude=True
    )

    async def instructions(self, manifest: Manifest) -> str:
        return (
            "Use apply_patch for sandbox file edits. Put the complete patch text in its JSON command field; "
            f"paths are relative to {manifest.root}. Read current contents with exec_command before editing. "
            "Use view_image to inspect a screenshot or image saved in the sandbox workspace."
        )

    def tools(self) -> list[FunctionTool]:
        if self.session is None:
            raise ValueError("Filesystem capability is not bound to a sandbox session")
        editor_tool = SandboxApplyPatchTool(session=self.session, user=self.run_as)

        @function_tool(failure_error_function=_patch_error)
        async def apply_patch(ctx: RunContextWrapper[Any], command: str) -> str:
            """Create, update or delete files in the sandbox workspace using a patch.

            Pass the patch as the JSON command string, not as a shell command.
            Begin with `*** Begin Patch` and finish with `*** End Patch`.
            Use `*** Add File: path` with +prefixed contents to create a file;
            `*** Update File: path` with @@ hunks and context/+/- lines to edit;
            or `*** Delete File: path` to delete. Paths are relative to the
            sandbox workspace. Parent directories are created as needed.

            Args:
                command: Complete patch text, including Begin Patch and End Patch markers.
            """
            # Delegate to the SDK's parser and session-backed workspace editor;
            # never interpret patch text in a host shell or open host files.
            output = await editor_tool.on_invoke_tool(ctx, command)
            return json.dumps({"success": True, "output": output}, ensure_ascii=False)

        image_tool = ViewImageTool(session=self.session, user=self.run_as)
        image_invoke = image_tool.on_invoke_tool

        async def view_image(ctx: RunContextWrapper[Any], arguments: str) -> Any:
            try:
                return await image_invoke(ctx, arguments)
            except Exception as exc:
                return json.dumps(
                    {
                        "error": f"{type(exc).__name__}: {exc}",
                        "hint": "Check the required path argument and use an image in the sandbox workspace.",
                    }
                )

        image_tool.on_invoke_tool = view_image
        tools = [apply_patch, image_tool]
        return self.configure_tools(tools) if self.configure_tools is not None else tools

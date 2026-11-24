import { fileURLToPath } from "node:url";

/** Hermes plugin name, as listed in `plugins.enabled`. */
export const pluginName = "slack-auto-join";

/** Directory to copy (without `__tests__/`) to `$HERMES_HOME/plugins/slack-auto-join/`. */
export const pluginDirectory = fileURLToPath(new URL(".", import.meta.url));

/** Files that make up the deployed plugin. */
export const pluginFiles = [
  "plugin.yaml",
  "__init__.py",
  "pyproject.toml",
  "_pythonmonkey.py",
  "policy.cjs",
] as const;

/** Bot events the Hermes Slack app must subscribe to for instant joins. */
export const manifestBotEvents = ["channel_created", "channel_unarchive"] as const;

/** User-token scopes for the optional private-channel inviter. */
export const userScopes = ["groups:read", "groups:write.invites"] as const;

/** `.env` variable holding that user token. */
export const userTokenEnv = "SLACK_AUTO_JOIN_USER_TOKEN";

interface Channel {
  id?: string;
  name?: string;
  is_archived?: boolean;
  is_private?: boolean;
  is_member?: boolean;
}

type Action = "member" | "join" | "invite" | "skip";
export function channelAction(channel: Channel, fromUser: boolean, knownPrivate: boolean): Action {
  if (!channel.id || channel.is_archived || knownPrivate) return "skip";
  if (fromUser) return "invite";
  if (channel.is_member) return "member";
  return channel.is_private ? "skip" : "join";
}

export function canInvite(hasClient: boolean, botId: string, team: string, inviterTeam: string) {
  return hasClient && Boolean(botId) && (!team || !inviterTeam || team === inviterTeam);
}

export function shouldRejoin(wasMember: boolean, left: boolean) {
  return !wasMember && !left;
}

interface Body {
  type?: string;
  team_id?: string;
  event?: {
    type?: string;
    team?: string;
    channel?: Channel | string;
  };
}

export function channelEvent({ type, team_id: team, event }: Body) {
  if (
    type !== "event_callback" ||
    !event ||
    !["channel_created", "channel_unarchive"].includes(event.type ?? "")
  )
    return;
  const channel = event.channel;
  if (!channel || (typeof channel === "object" && (!channel.id || channel.is_private))) return;
  return {
    team: team || event.team || "",
    id: typeof channel === "string" ? channel : channel.id,
    name: typeof channel === "string" ? "" : (channel.name ?? ""),
  };
}

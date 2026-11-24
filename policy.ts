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

interface SweepHost {
  nextBotChannel(): Promise<Channel | undefined>;
  nextPrivateChannel(): Promise<Channel | undefined>;
  inviterTeam(): Promise<string | undefined>;
  seenMember(id: string): Promise<void>;
  seenNonMember(id: string): Promise<boolean>;
  join(id: string, name: string): Promise<boolean>;
  invite(id: string, name: string): Promise<boolean>;
  noteLeft(id: string, name: string): Promise<void>;
  joined(id: string): Promise<void>;
  invited(id: string): Promise<void>;
}

export async function sweepTeam(host: SweepHost, team: string, botId: string, before: string[]) {
  const inPrivate = new Set<string>();
  const wasMember = new Set(before);
  for (let channel = await host.nextBotChannel(); channel; channel = await host.nextBotChannel()) {
    const { id, name = "" } = channel;
    if (!id) continue;
    switch (channelAction(channel, false, false)) {
      case "skip":
        break;
      case "member":
        await host.seenMember(id);
        if (channel.is_private) inPrivate.add(id);
        break;
      case "join":
        if (await host.seenNonMember(id)) {
          if (await host.join(id, name)) await host.joined(id);
        } else if (wasMember.has(id)) {
          await host.noteLeft(id, name);
        }
        break;
      case "invite":
        break;
    }
  }
  const inviterTeam = await host.inviterTeam();
  if (!canInvite(inviterTeam !== undefined, botId, team, inviterTeam ?? "")) return;
  for (
    let channel = await host.nextPrivateChannel();
    channel;
    channel = await host.nextPrivateChannel()
  ) {
    const { id, name = "" } = channel;
    if (!id || channelAction(channel, true, inPrivate.has(id)) !== "invite") continue;
    if (await host.seenNonMember(id)) {
      if (await host.invite(id, name)) await host.invited(id);
    } else if (wasMember.has(id)) {
      await host.noteLeft(id, name);
    }
  }
}

export function membershipResult(action: "join" | "invite", error: string, warning: string) {
  if (error === "ratelimited") return "retry";
  if (action === "invite" && error === "already_in_channel") return "alreadyInvited";
  if (error) return "failed";
  return warning === "already_in_channel" ? "alreadyJoined" : "joined";
}

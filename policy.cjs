Object.defineProperty(exports, Symbol.toStringTag, { value: "Module" });
// policy.ts
function channelAction(channel, fromUser, knownPrivate) {
	if (!channel.id || channel.is_archived || knownPrivate) return "skip";
	if (fromUser) return "invite";
	if (channel.is_member) return "member";
	return channel.is_private ? "skip" : "join";
}
function canInvite(hasClient, botId, team, inviterTeam) {
	return hasClient && Boolean(botId) && (!team || !inviterTeam || team === inviterTeam);
}
function shouldRejoin(wasMember, left) {
	return !wasMember && !left;
}
function channelEvent({ type, team_id: team, event }) {
	if (type !== "event_callback" || !event || !["channel_created", "channel_unarchive"].includes(event.type ?? "")) return;
	const channel = event.channel;
	if (!channel || typeof channel === "object" && (!channel.id || channel.is_private)) return;
	return {
		team: team || event.team || "",
		id: typeof channel === "string" ? channel : channel.id,
		name: typeof channel === "string" ? "" : channel.name ?? ""
	};
}
exports.canInvite = canInvite;
exports.channelAction = channelAction;
exports.channelEvent = channelEvent;
exports.shouldRejoin = shouldRejoin;

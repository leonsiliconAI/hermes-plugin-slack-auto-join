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
async function sweepTeam(host, team, botId, before) {
	const inPrivate = /* @__PURE__ */ new Set();
	const wasMember = new Set(before);
	for (let channel = await host.nextBotChannel(); channel; channel = await host.nextBotChannel()) {
		const { id, name = "" } = channel;
		if (!id) continue;
		switch (channelAction(channel, false, false)) {
			case "skip": break;
			case "member":
				await host.seenMember(id);
				if (channel.is_private) inPrivate.add(id);
				break;
			case "join": if (await host.seenNonMember(id)) {
				if (await host.join(id, name)) await host.joined(id);
			} else if (wasMember.has(id)) await host.noteLeft(id, name);
		}
	}
	const inviterTeam = await host.inviterTeam();
	if (!canInvite(inviterTeam !== void 0, botId, team, inviterTeam ?? "")) return;
	for (let channel = await host.nextPrivateChannel(); channel; channel = await host.nextPrivateChannel()) {
		const { id, name = "" } = channel;
		if (!id || channelAction(channel, true, inPrivate.has(id)) !== "invite") continue;
		if (await host.seenNonMember(id)) {
			if (await host.invite(id, name)) await host.invited(id);
		} else if (wasMember.has(id)) await host.noteLeft(id, name);
	}
}
function membershipResult(action, error, warning) {
	if (error === "ratelimited") return "retry";
	if (action === "invite" && error === "already_in_channel") return "alreadyInvited";
	if (error) return "failed";
	return warning === "already_in_channel" ? "alreadyJoined" : "joined";
}
exports.canInvite = canInvite;
exports.channelAction = channelAction;
exports.channelEvent = channelEvent;
exports.membershipResult = membershipResult;
exports.shouldRejoin = shouldRejoin;
exports.sweepTeam = sweepTeam;

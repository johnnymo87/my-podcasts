interface Env {
  BUCKET: R2Bucket;
  INBOX_QUEUE: Queue;
  GMAIL_FORWARD_TO: string;
  ALLOWED_SENDERS?: string;
}

interface InboxMessage {
  key: string;
  from: string;
  route_tag: string | null;
  route_source: "recipient" | "sender" | "list_id" | null;
  subject: string;
  date: string;
}

const DEFAULT_ALLOWED_SENDERS = [
  "noreply@news.bloomberg.com",
  "matthewyglesias@substack.com",
];

const SENDER_ROUTE_TAGS: Record<string, string> = {
  "noreply@news.bloomberg.com": "levine",
  "matthewyglesias@substack.com": "yglesias",
};

const LIST_ID_ROUTE_TAGS: Array<{ pattern: string; routeTag: string }> = [
  { pattern: "money stuff", routeTag: "levine" },
  { pattern: "slowboring", routeTag: "yglesias" },
  { pattern: "yglesias", routeTag: "yglesias" },
];

function getAllowedSenders(env: Env): string[] {
  const configured = env.ALLOWED_SENDERS
    ?.split(",")
    .map((s) => s.trim().toLowerCase())
    .filter((s) => s.length > 0);
  if (configured && configured.length > 0) {
    return configured;
  }
  return DEFAULT_ALLOWED_SENDERS;
}

function isAllowedSender(from: string, allowedSenders: string[]): boolean {
  const normalizedFrom = from.toLowerCase();
  return allowedSenders.some((sender) => normalizedFrom.includes(sender));
}

function routeTagFromRecipient(recipient: string): string | null {
  const [localPart] = recipient.split("@");
  const plusIndex = localPart.indexOf("+");
  if (plusIndex === -1) {
    return null;
  }
  const tag = localPart.slice(plusIndex + 1).trim().toLowerCase();
  return tag || null;
}

function extractEmailAddress(value: string): string {
  const angleMatch = /<([^>]+)>/.exec(value);
  if (angleMatch) {
    return angleMatch[1].trim().toLowerCase();
  }
  return value.trim().toLowerCase();
}

function routeTagFromSender(from: string): string | null {
  const senderEmail = extractEmailAddress(from);
  return SENDER_ROUTE_TAGS[senderEmail] ?? null;
}

function routeTagFromListId(listIdHeader: string | null): string | null {
  if (!listIdHeader) {
    return null;
  }
  const normalized = listIdHeader.toLowerCase();
  for (const mapping of LIST_ID_ROUTE_TAGS) {
    if (normalized.includes(mapping.pattern)) {
      return mapping.routeTag;
    }
  }
  return null;
}

function deriveRouteTag(
  message: ForwardableEmailMessage,
): { routeTag: string | null; routeSource: InboxMessage["route_source"] } {
  const recipientTag = routeTagFromRecipient(message.to);
  if (recipientTag) {
    return { routeTag: recipientTag, routeSource: "recipient" };
  }

  const senderTag = routeTagFromSender(message.from);
  if (senderTag) {
    return { routeTag: senderTag, routeSource: "sender" };
  }

  const listIdTag = routeTagFromListId(message.headers.get("list-id"));
  if (listIdTag) {
    return { routeTag: listIdTag, routeSource: "list_id" };
  }

  return { routeTag: null, routeSource: null };
}

export default {
  async email(message: ForwardableEmailMessage, env: Env): Promise<void> {
    const allowedSenders = getAllowedSenders(env);
    const forwardTo = env.GMAIL_FORWARD_TO;
    if (!forwardTo) {
      throw new Error("GMAIL_FORWARD_TO is not configured.");
    }

    await message.forward(forwardTo);

    if (!isAllowedSender(message.from, allowedSenders)) {
      return;
    }

    const { routeTag, routeSource } = deriveRouteTag(message);
    const key = `inbox/raw/${crypto.randomUUID()}.eml`;
    const rawBytes = new Uint8Array(await new Response(message.raw).arrayBuffer());
    await env.BUCKET.put(key, rawBytes, {
      httpMetadata: {
        contentType: "message/rfc822",
      },
      customMetadata: {
        from: message.from,
        route_tag: routeTag ?? "",
        route_source: routeSource ?? "",
        subject: message.headers.get("subject") ?? "No Subject",
        date: message.headers.get("date") ?? new Date().toISOString(),
      },
    });

    const payload: InboxMessage = {
      key,
      from: message.from,
      route_tag: routeTag,
      route_source: routeSource,
      subject: message.headers.get("subject") ?? "No Subject",
      date: message.headers.get("date") ?? new Date().toISOString(),
    };
    await env.INBOX_QUEUE.send(payload);
  },
};

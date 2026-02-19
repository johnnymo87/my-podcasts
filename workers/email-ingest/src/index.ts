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
  subject: string;
  date: string;
}

const DEFAULT_ALLOWED_SENDERS = [
  "noreply@news.bloomberg.com",
  "matthewyglesias@substack.com",
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

    const routeTag = routeTagFromRecipient(message.to);
    const key = `inbox/raw/${crypto.randomUUID()}.eml`;
    const rawBytes = new Uint8Array(await new Response(message.raw).arrayBuffer());
    await env.BUCKET.put(key, rawBytes, {
      httpMetadata: {
        contentType: "message/rfc822",
      },
      customMetadata: {
        from: message.from,
        route_tag: routeTag ?? "",
        subject: message.headers.get("subject") ?? "No Subject",
        date: message.headers.get("date") ?? new Date().toISOString(),
      },
    });

    const payload: InboxMessage = {
      key,
      from: message.from,
      route_tag: routeTag,
      subject: message.headers.get("subject") ?? "No Subject",
      date: message.headers.get("date") ?? new Date().toISOString(),
    };
    await env.INBOX_QUEUE.send(payload);
  },
};

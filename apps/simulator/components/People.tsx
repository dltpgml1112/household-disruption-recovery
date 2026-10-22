import { Fragment } from "react";

import type { Member } from "@/lib/types";

/** Each person writes in one pen colour (tokens --<id> / --<id>-soft in globals.css).
 *  Unknown ids get the neutral default, so colour never decides what a bar means. */
export function personClass(id: string | null | undefined): string {
  return id && /^[a-z][a-z0-9_-]*$/i.test(id) ? `p-${id.toLowerCase()}` : "";
}

export function Avatar({ member }: { member: Pick<Member, "id" | "name"> }) {
  return (
    <span className={`avatar ${personClass(member.id)}`} aria-hidden>
      {member.name.slice(0, 1)}
    </span>
  );
}

const escape = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

/** The text unchanged, with each household member's name underlined in their colour. */
export function Names({ text, members }: { text: string; members: Member[] }) {
  const named = members.filter((m) => m.name);
  if (!named.length) return <>{text}</>;
  const byName = new Map(named.map((m) => [m.name, m.id]));
  const parts = text.split(new RegExp(`\\b(${named.map((m) => escape(m.name)).join("|")})\\b`, "g"));
  return (
    <>
      {parts.map((p, i) =>
        i % 2 ? (
          <span key={i} className={`name ${personClass(byName.get(p))}`}>
            {p}
          </span>
        ) : (
          <Fragment key={i}>{p}</Fragment>
        ),
      )}
    </>
  );
}

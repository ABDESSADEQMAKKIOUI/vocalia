import { Fragment } from "react";

import { PRIVACY_URL, SUPPORT_EMAIL, SUPPORT_MAILTO, TERMS_URL } from "@/lib/support";

type FooterLink = { href: string; label: string; external: boolean };

export default function Footer() {
  // The legal pages are only linked once they actually exist (see @/lib/support).
  // The support address is always available, so the footer is never empty.
  const links: FooterLink[] = [
    ...(PRIVACY_URL
      ? [{ href: PRIVACY_URL, label: "Privacy Policy", external: true }]
      : []),
    ...(TERMS_URL
      ? [{ href: TERMS_URL, label: "Terms of Service", external: true }]
      : []),
    { href: SUPPORT_MAILTO, label: `Support: ${SUPPORT_EMAIL}`, external: false },
  ];

  return (
    <footer className="fixed bottom-0 left-0 right-0 bg-background border-t border-border py-4 px-6">
      <div className="flex justify-center items-center gap-6 text-sm text-muted-foreground">
        {links.map((link, index) => (
          <Fragment key={link.href}>
            {index > 0 && <span className="text-border">|</span>}
            <a
              href={link.href}
              {...(link.external
                ? { target: "_blank", rel: "noopener noreferrer" }
                : {})}
              className="hover:text-foreground transition-colors"
            >
              {link.label}
            </a>
          </Fragment>
        ))}
      </div>
    </footer>
  );
}

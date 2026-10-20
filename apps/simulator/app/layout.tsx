import type { Metadata } from "next";

import "./globals.css";

export const metadata: Metadata = {
  title: "Household Disruption Recovery",
  description: "Alexa+ simulation (unofficial): recover a family's morning after a school schedule change.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}

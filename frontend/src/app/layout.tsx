import type { Metadata } from "next";
import "./globals.css";
export const metadata: Metadata = {
  title: "SchemaShield · Migration safety",
  description:
    "Deterministic PostgreSQL migration analysis and controlled local validation.",
};
export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}

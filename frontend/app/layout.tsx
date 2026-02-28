import type { Metadata } from 'next';
import './globals.css';

export const metadata: Metadata = {
  title: 'Supply-Demand Allocator',
  description: 'Allocate supplies to competing demands',
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

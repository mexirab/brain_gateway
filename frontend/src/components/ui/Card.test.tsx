import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { Card } from './Card';

describe('Card (polymorphic)', () => {
  it('renders a <div> with the card class by default', () => {
    const { container } = render(<Card>content</Card>);
    const el = container.firstChild as HTMLElement;
    expect(el.tagName).toBe('DIV');
    expect(el).toHaveClass('card', 'p-5');
  });

  it('renders as an <a> link-card and forwards href', () => {
    render(
      <Card as="a" href="/finance">
        Budget
      </Card>,
    );
    const link = screen.getByRole('link', { name: 'Budget' });
    expect(link).toHaveAttribute('href', '/finance');
    expect(link).toHaveClass('card');
  });

  it('renders as a <form> wrapper', () => {
    const { container } = render(
      <Card as="form">
        <input />
      </Card>,
    );
    const form = container.querySelector('form');
    expect(form).not.toBeNull();
    expect(form).toHaveClass('card');
  });

  it('honours the padding prop', () => {
    const { container } = render(<Card padding="none">x</Card>);
    expect(container.firstChild).not.toHaveClass('p-5');
  });
});

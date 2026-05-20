import { ChangeDetectionStrategy, Component, computed, inject } from '@angular/core';
import { CommonModule } from '@angular/common';
import { ActivatedRoute } from '@angular/router';
import { toSignal } from '@angular/core/rxjs-interop';
import { TcgPlaceholderPageComponent } from './tcg-placeholder-page.component';

/**
 * Route-aware wrapper that reads `data.title` / `data.description` from
 * `ActivatedRoute` and renders a `<tcg-placeholder-page>`. Used by stub
 * routes in `tcgRoutes` so each path can carry its own copy via
 * `route.data` without needing a dedicated component per route.
 */
@Component({
  selector: 'tcg-placeholder-route',
  standalone: true,
  imports: [CommonModule, TcgPlaceholderPageComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: ` <tcg-placeholder-page [title]="title()" [description]="description()"></tcg-placeholder-page> `,
})
export class TcgPlaceholderRouteComponent {
  private readonly route = inject(ActivatedRoute);
  private readonly data = toSignal(this.route.data, { initialValue: { title: '', description: '' } });

  readonly title = computed<string>(() => (this.data() as { title?: string })?.title || 'Placeholder');
  readonly description = computed<string>(
    () => (this.data() as { description?: string })?.description || '',
  );
}

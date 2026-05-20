import { Component } from '@angular/core';
import { RouterOutlet } from '@angular/router';
import { TcgSidebarComponent } from '@tcg/frontend';

@Component({
  selector: 'app-root',
  imports: [RouterOutlet, TcgSidebarComponent],
  templateUrl: './app.component.html',
  styleUrl: './app.component.css',
})
export class AppComponent {
  title = 'dev-harness';
  collapsed = false;

  onSidebarToggled(next: boolean): void {
    this.collapsed = next;
    // Plotly resize hack — fire after the sidebar transition completes so
    // any chart in the main pane re-fits the new viewport width. Matches
    // the React app's `setTimeout(..., 260)` pattern.
    setTimeout(() => window.dispatchEvent(new Event('resize')), 260);
  }
}

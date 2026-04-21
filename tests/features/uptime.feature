Feature: Uptime calculation
  As a user reporting reliability data,
  I want a single uptime percentage over a window,
  So that I can summarize the customer-visible reliability of my line.

  Scenario: Perfect uptime
    Given a probe stream "OOOOO" at 5-second intervals
    When I compute uptime
    Then the uptime is 100.0 percent

  Scenario: Half failures
    Given a probe stream "O.O.O.O." at 5-second intervals
    When I compute uptime
    Then the uptime is 50.0 percent

  Scenario: Empty window reports 100 percent
    Given no probes
    When I compute uptime
    Then the uptime is 100.0 percent

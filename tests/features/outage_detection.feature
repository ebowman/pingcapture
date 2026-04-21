Feature: Outage detection
  As a user trying to characterize my DSL connection,
  I want consecutive ping failures to be grouped into a single "outage" event,
  So that brief flickers don't pollute my report and real outages are obvious.

  Scenario: A short string of failures becomes one outage
    Given a probe stream "OOO...OOO" at 5-second intervals
    When I detect outages
    Then I see 1 outage
    And the outage duration is 15 seconds

  Scenario: A single transient failure is ignored
    Given a probe stream "OO.OO" at 5-second intervals
    When I detect outages
    Then I see 0 outages

  Scenario: Two distinct outages in one window
    Given a probe stream "OO...OOOO...OO" at 5-second intervals
    When I detect outages
    Then I see 2 outages

  Scenario: A long quiet gap is treated as the end of an outage
    Given a probe stream "..." at 5-second intervals
    And then a long quiet period of 600 seconds
    And then a probe stream "OO" at 5-second intervals
    When I detect outages
    Then I see 1 outage
    And the outage duration is 10 seconds

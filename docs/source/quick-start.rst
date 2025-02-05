Quick start
+++++++++++

Some useful examples.

Simple consumer
~~~~~~~~~~~~~~~

.. literalinclude:: examples/simple_consumer.py
   :language: python

Simple publisher
~~~~~~~~~~~~~~~~

.. literalinclude:: examples/simple_publisher.py
   :language: python

Asynchronous message processing
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. literalinclude:: examples/simple_async_consumer.py
   :language: python


Working with RabbitMQ transactions
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. literalinclude:: examples/simple_publisher_transactions.py
   :language: python

Get single message example
~~~~~~~~~~~~~~~~~~~~~~~~~~

.. literalinclude:: examples/main.py
   :language: python

Set logging level
~~~~~~~~~~~~~~~~~

Sometimes you want to see only your debug logs, but when you just call
`logging.basicConfig(logging.DEBUG)` you set the debug log level for all
loggers, includes all aio_pika's modules. If you want to set logging level
independently see following example:

.. literalinclude:: examples/log-level-set.py
   :language: python

Tornado example
~~~~~~~~~~~~~~~

.. literalinclude:: examples/tornado-pubsub.py
   :language: python

SSL connection example
~~~~~~~~~~~~~~~~~~~~~~

.. literalinclude:: examples/amqps-connection.py
   :language: python

External credentials example
~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. literalinclude:: examples/external-credentials.py
   :language: python

Connection pooling
~~~~~~~~~~~~~~~~~~

.. literalinclude:: examples/pooling.py
   :language: python
